"""How many requests have we actually sent to the LLM?

The complaint this answers is "request overload": a single chat turn can fire
far more model calls than the user sent messages — every ReAct step is a call,
retries are calls, a condense is a call, a fold is a call — and none of it was
visible. This counts REQUESTS AT THE WIRE (one per HTTP attempt, retries
included, which is what a rate-limited provider counts too) and attributes them
to the chat session that caused them.

Deliberately in-process and bounded: this is a live meter for the UI, not
billing. Counters reset when the API restarts, and only the most recent
``_MAX_SESSIONS`` sessions are kept.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

_MAX_SESSIONS = 200
# Timestamps of recent calls, for the "requests in the last minute" rate. One
# float each; the deque is trimmed on read, and hard-bounded so a runaway
# cannot grow it without limit.
_RECENT_MAX = 20_000
_WINDOW_S = 60.0

_lock = threading.Lock()
_total = 0
_recent: deque = deque(maxlen=_RECENT_MAX)
_sessions: "OrderedDict[str, dict]" = OrderedDict()


def _key(session_id) -> "str | None":
    return None if session_id in (None, "") else str(session_id)


def _slot(sid: str) -> dict:
    slot = _sessions.get(sid)
    if slot is None:
        slot = {"total": 0, "turn": 0, "by_role": {}}
        _sessions[sid] = slot
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.popitem(last=False)     # oldest out
    else:
        _sessions.move_to_end(sid)
    return slot


def record(role: str | None = None, session_id=None, *, now: float | None = None) -> None:
    """One request went out. Never raises — metering must not break a call."""
    global _total
    try:
        ts = time.monotonic() if now is None else now
        sid = _key(session_id)
        if sid is None or not role:
            # The wire-level caller (_post) knows neither — both ride the
            # request context, which the chat turn binds and the generation
            # thread inherits (see _complete_cancellable).
            try:
                from aiforge_core.runtime import request_context
                if sid is None:
                    sid = _key(request_context.get_session_id())
                role = role or request_context.get_role()
            except Exception:  # noqa: BLE001
                pass
        with _lock:
            _total += 1
            _recent.append(ts)
            if sid is not None:
                slot = _slot(sid)
                slot["total"] += 1
                slot["turn"] += 1
                if role:
                    slot["by_role"][role] = slot["by_role"].get(role, 0) + 1
    except Exception:  # noqa: BLE001
        pass


def turn_reset(session_id) -> None:
    """Start a new turn for this session — the per-turn counter goes back to 0
    while the session total keeps climbing."""
    sid = _key(session_id)
    if sid is None:
        return
    with _lock:
        _slot(sid)["turn"] = 0


def _per_minute_locked(now: float) -> int:
    cutoff = now - _WINDOW_S
    while _recent and _recent[0] < cutoff:
        _recent.popleft()
    return len(_recent)


def snapshot(session_id=None) -> dict:
    """Live counts: this turn, this session, this process, and the rate over
    the last minute (across ALL sessions — the machine's load is what the user
    feels, not one chat's share of it)."""
    sid = _key(session_id)
    now = time.monotonic()
    with _lock:
        slot = _sessions.get(sid) if sid is not None else None
        return {
            "turn": int((slot or {}).get("turn") or 0),
            "session": int((slot or {}).get("total") or 0),
            "total": _total,
            "per_minute": _per_minute_locked(now),
            "by_role": dict((slot or {}).get("by_role") or {}),
        }


def reset_all() -> None:
    """Test helper — drop every counter."""
    global _total
    with _lock:
        _total = 0
        _recent.clear()
        _sessions.clear()


__all__ = ["record", "turn_reset", "snapshot", "reset_all"]
