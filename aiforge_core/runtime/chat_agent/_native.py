"""Native OpenAI tool-calling for the simple chat loop.

The text ACTION/ARGS_JSON protocol makes local models emit ``ARGS_JSON: {}``
(arg-less tool calls). Native function-calling — what OpenWebUI uses on the same
endpoints — returns real structured arguments. This module decides WHEN to use
native (per-model capability probe + env override) and adapts a native reply
back into the exact text step the existing loop already parses — so the whole
loop (dispatch, edit-guard, verify, compaction) is reused unchanged; native FC
only changes HOW the next step is produced. Text protocol stays the fallback.
"""
from __future__ import annotations

import json
import os

# model id -> native-tool support (probed once, then cached).
_NATIVE_CACHE: dict[str, bool] = {}


def _protocol_setting() -> str:
    """``native`` | ``text`` | ``auto`` (default). auto probes the model once."""
    v = (os.environ.get("AIFORGE_CHAT_TOOL_PROTOCOL", "auto") or "auto").strip().lower()
    return v if v in ("native", "text", "auto") else "auto"


def reset_native_cache() -> None:
    _NATIVE_CACHE.clear()


def _probe_timeout() -> int:
    try:
        return int(os.environ.get("AIFORGE_CHAT_NATIVE_PROBE_TIMEOUT_S", "12"))
    except (TypeError, ValueError):
        return 12


def _model_for(role: str) -> str:
    try:
        from aiforge_core.llm.router import resolve
        return resolve(role).model or role
    except Exception:  # noqa: BLE001
        return role


def _probe_native(role: str) -> bool:
    """Force one tiny tool call; the endpoint supports native FC iff it returns
    a ``tool_calls`` reply. A server without tool support errors (4xx) → False.
    Cached per model so it costs one probe. Never raises."""
    model = _model_for(role)
    if model in _NATIVE_CACHE:
        return _NATIVE_CACHE[model]
    from aiforge_core.llm import client
    tools = [{"type": "function", "function": {
        "name": "aiforge_ping", "description": "Acknowledge readiness.",
        "parameters": {"type": "object",
                       "properties": {"ack": {"type": "string"}},
                       "required": []}}}]
    msgs = [{"role": "user", "content": "Call the aiforge_ping tool with ack='ok'."}]
    try:
        m = client.complete_raw(
            role, msgs, tools=tools,
            tool_choice={"type": "function", "function": {"name": "aiforge_ping"}},
            max_tokens=64, timeout_s=_probe_timeout())
        ok = bool(m.get("tool_calls"))
    except Exception:  # noqa: BLE001 — no tool support / transport → text protocol
        ok = False
    _NATIVE_CACHE[model] = ok
    return ok


def native_tools_enabled(role: str) -> bool:
    """True when the simple loop should use native tool-calling for ``role``.
    ``native``/``text`` force it; ``auto`` (default) probes the model once."""
    s = _protocol_setting()
    if s == "text":
        return False
    if s == "native":
        return True
    return _probe_native(role)


def _synth_step(msg: dict) -> str:
    """Adapt a native assistant message into the text step the loop's ``_parse``
    already understands. A ``tool_calls`` reply → a synthetic ACTION/ARGS_JSON
    line carrying the REAL structured args (no more ``ARGS_JSON: {}``); a plain
    reply → its content verbatim (parsed as FINAL/ASK/text as before). Only the
    FIRST tool call is taken — the loop runs one action per turn."""
    calls = msg.get("tool_calls") or []
    if calls:
        fn = (calls[0] or {}).get("function") or {}
        name = fn.get("name") or ""
        raw = fn.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (ValueError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        return f"ACTION: {name}\nARGS_JSON: {json.dumps(args, ensure_ascii=False)}"
    content = msg.get("content") or ""
    return content if isinstance(content, str) else str(content)


def make_native_complete_fn():
    """A drop-in ``complete_fn(role, convo) -> str`` that calls the model with
    native tools and returns the adapted text step. Passing the CORE tool
    schemas natively while the full tool catalog stays in the system prompt is
    deliberate: core coding tools get reliable native args, the long tail is
    still callable via a text ACTION in the same turn (hybrid)."""
    from aiforge_core.llm import client

    from ._tools._schemas import NATIVE_TOOL_SCHEMAS

    def _fn(role: str, convo: list[dict]) -> str:
        msg = client.complete_raw(
            role, convo, tools=NATIVE_TOOL_SCHEMAS, tool_choice="auto")
        return _synth_step(msg)

    return _fn
