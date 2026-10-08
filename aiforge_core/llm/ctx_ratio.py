"""How many characters one prompt token really is, as the provider counted it.

The context budget is kept in characters and was converted at a fixed 4
characters per token. Code, JSON and tool output run nearer 3, and the tool
list the model is sent with every request is not in the conversation at all,
so on a small window the conversation reached the model's limit before the
budget said it was full: the server refused or cut the prompt, and the turn
restarted from a forced condense.

Every response says how many prompt tokens it took. Dividing the characters of
the messages that were sent by that number gives a ratio that already pays for
the tool list and the system prompt. :func:`chars_per_token` hands it to the
budget; with no fresh measure it answers ``None`` and the caller keeps 4.

``AIFORGE_CTX_MEASURED=0`` turns it off.
"""
from __future__ import annotations

import contextvars
import os
import threading
import time

#: A measure from a prompt this small is mostly the fixed part (tool list,
#: system prompt) and says little about the conversation.
_MIN_TOKENS = 2000
#: Outside these bounds the count is a misreport (a proxy that returns 0, a
#: cached-prompt count), not a ratio.
_LOW, _HIGH = 1.2, 6.0
#: A measure older than this is not trusted (the ratio is mostly a property
#: of the model and the kind of text, so it lives long).
_MAX_AGE_S = 6 * 3600.0

_lock = threading.Lock()
_last: dict = {}
#: The chat the running turn belongs to: a measure is kept per chat, because
#: the tool list is a fixed cost and a short chat's ratio would shrink a long
#: chat's budget.
_SESSION: contextvars.ContextVar = contextvars.ContextVar("aiforge_ctx_ratio_session", default=None)


def bind(session_id):
    """Measures from here on belong to ``session_id``. Returns a token for
    :func:`unbind`."""
    return _SESSION.set(session_id)


def unbind(token) -> None:
    try:
        _SESSION.reset(token)
    except (ValueError, LookupError):
        pass


def _key(role):
    return (role or "", _SESSION.get())


def enabled() -> bool:
    return os.environ.get("AIFORGE_CTX_MEASURED", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _chars(messages) -> int:
    total = 0
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += len(part["text"])
        for call in m.get("tool_calls") or ():
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                total += len(str(fn.get("arguments") or "")) + len(str(fn.get("name") or ""))
    return total


def note(role, messages, prompt_tokens, model=None) -> None:
    """Remember the ratio of one request (``model``: what the provider says
    answered it — another model has another tokenizer). Never raises."""
    try:
        tokens = int(prompt_tokens or 0)
        if tokens < _MIN_TOKENS:
            return
        ratio = _chars(messages) / tokens
        if not _LOW <= ratio <= _HIGH:
            return
        with _lock:
            _last[_key(role)] = (ratio, time.monotonic(), str(model or ""))
    except Exception:  # noqa: BLE001 — accounting never breaks a call
        pass


def _current_model(role) -> str:
    try:
        from aiforge_core.llm.router import resolve
        return str(getattr(resolve(role), "model", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _same_model(measured: str, current: str) -> bool:
    """Providers name a model their own way ("openai/x", "x", "x-q4"): the
    measure counts when either name contains the other's last part."""
    if not measured or not current:
        return True
    a, b = measured.split("/")[-1].lower(), current.split("/")[-1].lower()
    return a in b or b in a


def chars_per_token(role=None) -> "float | None":
    """The last fresh ratio for ``role``, else ``None`` (also when it was
    measured on another model than the role uses now)."""
    if not enabled():
        return None
    with _lock:
        hit = _last.get(_key(role))
    if not hit:
        return None
    ratio, at, model = hit
    if time.monotonic() - at > _MAX_AGE_S:
        return None
    if not _same_model(model, _current_model(role)):
        return None
    return ratio


def seed(role, ratio) -> None:
    """A ratio measured earlier in this chat (saved with its transcript),
    used until this turn measures its own — the first call of a carried turn
    must not be sized at the 4 characters-per-token guess."""
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return
    if not _LOW <= r <= _HIGH:
        return
    with _lock:
        _last.setdefault(_key(role), (r, time.monotonic(), _current_model(role)))


# ── what a "too long" refusal says ──────────────────────────────────────────

import re as _re

#: "maximum context length is 131072 tokens", "n_ctx: 125000", "context
#: length of 32768", "context size (125000)".
_MAX_RE = _re.compile(
    r"(?:maximum context length|context length|context window|context size|n_ctx"
    r"|max(?:imum)? (?:prompt|input) length)\D{0,24}?(\d{3,7})", _re.I)
#: "you requested 140000 tokens", "prompt has 130512 tokens", "input length
#: 128900", "(130000 > 125000)".
_ASKED_RE = _re.compile(
    r"(?:requested|prompt (?:has|contains|is)|input (?:length|has|is)|resulted in)"
    r"\D{0,16}?(\d{3,7})|\((\d{3,7})\s*>\s*\d{3,7}\)", _re.I)

_windows: dict = {}
#: A "window" smaller than this in a refusal is more likely a proxy's or a
#: fallback model's limit than the chat model's: not learned.
_MIN_WINDOW = 8192
#: A learned window is trusted this long (a server can be reloaded with more).
_WINDOW_AGE_S = 3600.0


def learn_from_overflow(role, messages, error_text: str) -> bool:
    """The server refused the prompt as too long; learn from its numbers.
    How many tokens it counted for our characters gives the real ratio; its
    window, when smaller than the configured one, becomes the window (a
    setting of 262K on a model loaded with 125K). When it names neither, the
    ratio is set so that this prompt counts as 10% over the window. True when
    something was learned."""
    # "131,072" and "131_072" are one number.
    text = _re.sub(r"(?<=\d)[,_](?=\d{3}\b)", "", str(error_text or ""))
    m_max = _MAX_RE.search(text)
    m_ask = _ASKED_RE.search(text)
    window = int(m_max.group(1)) if m_max else 0
    asked = int(next((g for g in (m_ask.groups() if m_ask else ()) if g), 0) or 0)
    chars = _chars(messages)
    learned = False
    if window >= _MIN_WINDOW:
        with _lock:
            _windows[_key(role)] = (window, time.monotonic(), _current_model(role))
        learned = True
    tokens = asked or (int(window * 1.1) if window else 0)
    if chars and tokens >= _MIN_TOKENS:
        ratio = chars / tokens
        if _LOW <= ratio <= _HIGH:
            with _lock:
                _last[_key(role)] = (ratio, time.monotonic(), "")
            learned = True
    return learned


def learned_window(role=None) -> int:
    """The window the server said it has, for this role and chat, or 0."""
    with _lock:
        hit = _windows.get(_key(role))
    if not hit or time.monotonic() - hit[1] > _WINDOW_AGE_S:
        return 0
    if not _same_model(hit[2], _current_model(role)):
        return 0                 # learned on another model
    return hit[0]


def reset() -> None:
    with _lock:
        _last.clear()
        _windows.clear()
