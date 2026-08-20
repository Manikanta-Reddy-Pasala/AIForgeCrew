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

import contextvars
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
# Which TURN a call belongs to. Inherited by any context copy (the generation
# thread), so a cancelled turn's abandoned retries cannot land on the next one.
_TURN_EPOCH: "contextvars.ContextVar[int | None]" = contextvars.ContextVar(
    "aiforge_turn_epoch", default=None)
_total = 0
_recent: deque = deque(maxlen=_RECENT_MAX)
_sessions: "OrderedDict[str, dict]" = OrderedDict()


def _key(session_id) -> "str | None":
    return None if session_id in (None, "") else str(session_id)


def _slot(sid: str) -> dict:
    slot = _sessions.get(sid)
    if slot is None:
        slot = {"total": 0, "turn": 0, "by_role": {}, "epoch": 0}
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
        sid = _key(session_id)
        epoch = None
        if sid is None or not role:
            # The wire-level caller (_post) knows neither — both ride the
            # request context, which the chat turn binds and the generation
            # thread inherits (see _complete_cancellable).
            #
            # CONTEXTVAR ONLY. request_context.get_session_id() also falls back
            # to the process-global AIFORGE_CURRENT_SESSION env var, which the
            # chat route sets and never clears — so every bare thread in the
            # system (session folds, learners, classifiers) would bill its
            # calls to whichever chat last ran a turn. An unattributed call is
            # correct; a call billed to an innocent chat is not.
            try:
                from aiforge_core.runtime import request_context
                if sid is None:
                    sid = _key(request_context.context_session_id())
                role = role or request_context.get_role()
            except Exception:  # noqa: BLE001
                pass
        try:
            epoch = _TURN_EPOCH.get()
        except Exception:  # noqa: BLE001
            epoch = None
        with _lock:
            # Stamped INSIDE the lock: two threads racing between "read clock"
            # and "append" can invert the deque, and the trim below stops at
            # the first entry inside the window — one stale entry parked behind
            # a newer one would freeze the whole trim.
            _total += 1
            _recent.append(time.monotonic() if now is None else now)
            if sid is not None:
                slot = _slot(sid)
                slot["total"] += 1
                # A cancelled generation is ABANDONED, not stopped: its thread
                # keeps retrying with the turn's context still bound. Those
                # calls belong to the turn that made them, not to whatever the
                # user typed next — so the per-turn counter only accepts calls
                # stamped with the CURRENT turn's epoch (None = a caller
                # outside any turn, e.g. a background fold).
                if epoch is None or epoch == slot["epoch"]:
                    slot["turn"] += 1
                if role:
                    slot["by_role"][role] = slot["by_role"].get(role, 0) + 1
    except Exception:  # noqa: BLE001
        pass


def turn_reset(session_id):
    """Start a new turn for this session — the per-turn counter goes back to 0
    while the session total keeps climbing.

    Returns a token to pass to :func:`bind_turn` (or None). Call this at the
    START of the turn, in the route, BEFORE the enhancer/classifier calls: they
    are requests the user's message caused, and resetting later erased them.
    """
    sid = _key(session_id)
    if sid is None:
        return None
    with _lock:
        slot = _slot(sid)
        slot["turn"] = 0
        slot["epoch"] += 1
        return (sid, slot["epoch"])


def bind_turn(token):
    """Stamp this context (and any thread that copies it) with the turn the
    calls belong to. Returns a contextvars Token, or None."""
    if not token:
        return None
    try:
        return _TURN_EPOCH.set(token[1])
    except Exception:  # noqa: BLE001
        return None


def reset_turn(cv_token) -> None:
    if cv_token is None:
        return
    try:
        _TURN_EPOCH.reset(cv_token)
    except Exception:  # noqa: BLE001
        pass


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
        slot = _slot(sid) if sid is not None and sid in _sessions else None
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


__all__ = ["record", "turn_reset", "bind_turn", "reset_turn", "snapshot",
           "reset_all"]
