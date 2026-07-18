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
    # A 5xx / 429 is transient (busy / loading / rate-limited), NEVER a
    # definitive tools-rejection — check the code FIRST, before consuming the
    # one-shot body, so a transient error can't disable native.
    code = getattr(exc, "code", 0)
    if code == 429 or (isinstance(code, int) and code >= 500):
        return False
    # A tools-incapable OpenAI-compatible endpoint returns HTTP 400 whose REASON
    # is in the response BODY — `str(exc)` alone is just "HTTP Error 400: Bad
    # Request" (no tool/reject word). Classify on str(exc) + the HTTP body.
    try:
        from aiforge_core.llm.client._errors import _http_err_body
        body = _http_err_body(exc)
    except Exception:  # noqa: BLE001
        body = ""
    m = (str(exc) + " " + body).lower()
    mentions_tools = "tool" in m or "function" in m
    # DELIBERATELY excludes generic 400 words — 'invalid' (every OpenAI-compatible
    # 400 body carries `"type":"invalid_request_error"`), 'unknown' ('Unknown
    # parameter'), 'unrecognized'/'unexpected' (Jackson 'Unrecognized field') —
    # each of which, paired with a tools-schema echo (contains "function"), would
    # falsely + PERMANENTLY disable native. The tokens below name a real
    # tools-CAPABILITY rejection and do NOT appear in a generic 400.
    rejected = any(t in m for t in (
        "unsupported", "not support", "does not support", "no such tool",
        "no such function", "not allowed", "not implemented", "not capable",
        "tools are disabled", "function calling is disabled"))
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
        if _tools_unsupported(exc) and not _rejects_only_tool_choice(exc):
            _NATIVE_CACHE[model] = False   # definitive: endpoint rejects tools
            return False
        # A rejection that names ONLY tool_choice (the probe forces
        # tool_choice="required"; some servers do tools with "auto" but reject
        # the forced mode the RUNTIME never uses) is NOT a tools-capability
        # rejection — the model can do native FC. Stay optimistic, don't cache.
        # Also inconclusive (busy / timeout / reloading).
        log.info("native probe inconclusive (%s) — staying optimistic", exc)
        return True


def _rejects_only_tool_choice(exc: Exception) -> bool:
    """True when the rejection is specifically about the ``tool_choice`` PARAMETER
    (the forced-call mode the probe uses), not about tools support in general —
    so a model that does native FC with ``tool_choice="auto"`` isn't wrongly
    disabled just because it refuses ``"required"``."""
    try:
        from aiforge_core.llm.client._errors import _http_err_body
        m = (str(exc) + " " + _http_err_body(exc)).lower()
    except Exception:  # noqa: BLE001
        m = str(exc).lower()
    return "tool_choice" in m or "tool choice" in m


def native_tools_enabled(role: str) -> bool:
    """True when the simple loop should use native tool-calling for ``role``.
    ``native``/``text`` force it; ``auto`` (default) probes the model once."""
    s = _protocol_setting()
    if s == "text":
        return False
    # A model that hit a DEFINITIVE tools-rejection on a prior turn is cached
    # False — don't re-wire native (and don't emit the 'native active' banner
    # for a run that will silently run on text).
    if _NATIVE_CACHE.get(_model_for(role)) is False:
        return False
    if s == "native":
        return True
    return _probe_native(role)


# Sentinel: a tool_call whose arguments were ATTEMPTED but are unrecoverable
# (truncated/malformed JSON). Emitting ``ARGS_JSON: {}`` would silently drop the
# model's real args — the exact empty-args failure this feature kills — so we
# signal the caller to redo the turn on the hardened text path instead.
_NATIVE_ARGS_UNRECOVERABLE = "\x00native-args-unrecoverable"


def _synth_step(msg: dict) -> str:
    """Adapt a native assistant message into the text step the loop's ``_parse``
    already understands. A ``tool_calls`` reply → a synthetic ACTION/ARGS_JSON
    line carrying the REAL structured args; a plain reply → its recovered text
    (content, else the reasoning channel, think-stripped). Only the FIRST tool
    call is taken — the loop runs one action per turn. Returns the
    ``_NATIVE_ARGS_UNRECOVERABLE`` sentinel when a named call's arguments were
    attempted but can't be parsed (caller falls back to text for that turn)."""
    from aiforge_core.llm.client._text import _msg_text
    calls = msg.get("tool_calls") or []
    if calls:
        fn = (calls[0] or {}).get("function") or {}
        name = fn.get("name") or ""
        raw = fn.get("arguments")
        # Resolve args, DISTINGUISHING a legit empty-args call from a malformed
        # one: dict → use it; None/""/"{}" → genuinely empty; a non-empty string
        # that fails to parse → ATTEMPTED-but-broken (don't surrender to {}).
        if isinstance(raw, dict):
            args = raw
        elif raw is None or (isinstance(raw, str) and raw.strip() in ("", "{}")):
            args = {}
        elif isinstance(raw, str):
            try:
                args = json.loads(raw)
            except (ValueError, TypeError):
                args = None
        else:
            args = None
        # A nameless call carries no action → treat it as plain content.
        if not name:
            return _msg_text(msg)
        if not isinstance(args, dict):
            return _NATIVE_ARGS_UNRECOVERABLE
        return f"ACTION: {name}\nARGS_JSON: {json.dumps(args, ensure_ascii=False)}"
    return _msg_text(msg)


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
            if _tools_unsupported(exc) and not _rejects_only_tool_choice(exc):
                # DEFINITIVE: this model can't do native tools — cache + fall
                # back to text for this and every future turn. A rejection that
                # names ONLY tool_choice is NOT a tools-capability signal (same
                # guard the probe uses) — the model does native FC; don't disable.
                _NATIVE_CACHE[model] = False
                log.info("native unsupported at runtime → text fallback (%s)", model)
                return client.complete(role, convo)
            # A clearly TRANSIENT error (5xx/429/timeout/model-reloading) → let
            # the loop's retry re-issue native. Anything else (an unclassified
            # 400 — e.g. a strict/Jackson server rejecting the 'tools' field with
            # wording our token list doesn't recognise) must NOT hard-fail every
            # turn: fall back to text for THIS turn (no permanent disable — we
            # re-probe native next turn).
            try:
                from aiforge_core.llm.client._errors import _is_transient_exc
                transient = bool(_is_transient_exc(exc)[0])
            except Exception:  # noqa: BLE001
                transient = False
            if transient:
                raise
            log.info("native call failed non-transiently → text this turn (%s)", exc)
            return client.complete(role, convo)
        calls = msg.get("tool_calls") or []
        # Observability: log whether this native step produced a tool_call or
        # plain content so a run can be audited ("all calls native").
        if calls:
            fn = (calls[0] or {}).get("function") or {}
            log.info("native tool_call: %s (n=%d)", fn.get("name"), len(calls))
        else:
            log.info("native content step (no tool_call)")
        step = _synth_step(msg)
        if step == _NATIVE_ARGS_UNRECOVERABLE:
            # the model attempted tool args but they were truncated/malformed —
            # redo this turn on the hardened text path rather than emit empty args
            log.info("native args unrecoverable → text fallback for this turn")
            return client.complete(role, convo)
        return step

    return _fn
