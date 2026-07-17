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
import logging
import os

log = logging.getLogger("aiforge.chat.native")

# model id -> native-tool support (probed once, then cached).
_NATIVE_CACHE: dict[str, bool] = {}


def _protocol_setting() -> str:
    """``native`` (DEFAULT) | ``text`` | ``auto``. Native is the default
    everywhere — the text ACTION/ARGS_JSON protocol is the fumble-prone legacy
    path. ``auto`` probes the model once; ``text`` forces the legacy protocol.
    Native self-heals to text at runtime only on a DEFINITIVE tools-rejection
    (see :func:`make_native_complete_fn`), so defaulting native is safe even for
    an occasional model that can't do tools."""
    v = (os.environ.get("AIFORGE_CHAT_TOOL_PROTOCOL", "native") or "native").strip().lower()
    return v if v in ("native", "text", "auto") else "native"


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


def _tools_unsupported(exc: Exception) -> bool:
    """A DEFINITIVE 'this endpoint can't do native tools' signal (vs a transient
    busy/timeout/transport error). Only a tools/function rejection counts — a
    timeout or connection drop on a model that's merely loading is inconclusive
    and must NOT permanently disable native."""
    m = str(exc).lower()
    mentions_tools = "tool" in m or "function" in m
    rejected = any(t in m for t in (
        "unsupported", "not support", "does not support", "unknown", "invalid",
        "no such", "not allowed", "not implemented"))
    return mentions_tools and rejected


def _probe_native(role: str) -> bool:
    """Force one tiny tool call; the endpoint supports native FC iff it returns
    a ``tool_calls`` reply. Result is cached per model — but ONLY a definitive
    outcome (a real response, or a tools-rejection error). A transient failure
    (timeout / busy / model reloading) is inconclusive: it is NOT cached and we
    stay OPTIMISTIC (return True) so a momentary endpoint hiccup can't disable
    native for the whole process lifetime (it re-confirms on the next turn).
    Never raises."""
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
    # tool_choice MUST be a string ("none"/"auto"/"required") — LM Studio (and
    # some other OpenAI-compatible servers) reject the object form with HTTP 400
    # ("Invalid tool_choice type: 'object'"). "required" forces a call so the
    # probe gets a deterministic positive signal on a tool-capable endpoint.
    try:
        m = client.complete_raw(
            role, msgs, tools=tools, tool_choice="required",
            max_tokens=64, timeout_s=_probe_timeout())
        ok = bool(m.get("tool_calls"))
        _NATIVE_CACHE[model] = ok          # definitive: endpoint responded
        return ok
    except Exception as exc:  # noqa: BLE001
        if _tools_unsupported(exc):
            _NATIVE_CACHE[model] = False   # definitive: endpoint rejects tools
            return False
        # Inconclusive (busy / timeout / reloading) — don't cache, stay native.
        log.info("native probe inconclusive (%s) — staying optimistic", exc)
        return True


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
        # Known-incapable model (a prior turn hit a definitive tools-rejection)
        # → text protocol, transparently. This is the ONLY thing that disables
        # native, and it's per-model + self-discovered, never transient.
        model = _model_for(role)
        if _NATIVE_CACHE.get(model) is False:
            return client.complete(role, convo)
        try:
            msg = client.complete_raw(
                role, convo, tools=NATIVE_TOOL_SCHEMAS, tool_choice="auto")
        except Exception as exc:  # noqa: BLE001
            if _tools_unsupported(exc):
                # DEFINITIVE: this model can't do native tools — cache + fall
                # back to text for this and every future turn.
                _NATIVE_CACHE[model] = False
                log.info("native unsupported at runtime → text fallback (%s)", model)
                return client.complete(role, convo)
            raise  # transient (busy/timeout) → let the loop's retry handle it
        calls = msg.get("tool_calls") or []
        # Observability: log whether this native step produced a tool_call or
        # plain content so a run can be audited ("all calls native").
        if calls:
            fn = (calls[0] or {}).get("function") or {}
            log.info("native tool_call: %s (n=%d)", fn.get("name"), len(calls))
        else:
            log.info("native content step (no tool_call)")
        return _synth_step(msg)

    return _fn
