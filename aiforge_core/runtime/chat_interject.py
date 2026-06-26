"""Per-session mid-run steering queue for chat runs (Gap A).

While a chat turn is in flight (the simple/plan ReAct loop in
``chat_agent.run_chat_agent`` or the team-mode ADK driver in
``chat_pipeline``), the user can inject a guidance message WITHOUT pressing
Stop. The message is queued here and folded into the agent's working
context at the next safe step, so the agent adjusts course mid-run.

Modeled on ``chat_cancel``: per-session, thread-safe (one lock + dict),
cleared on ``finish``/``clear`` so stale steers can't leak into the next
turn.

Usage:
    chat_interject.push(session_id, "actually, also handle the empty case")
    ...                                   # from the /steer endpoint
    for text in chat_interject.drain(session_id):   # in the run loop
        convo.append({"role": "user", "content": f"[steer] {text}"})
    ...
    chat_interject.clear(session_id)      # alongside chat_cancel.finish
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_QUEUES: dict[int, list[str]] = {}
# Sessions whose in-flight run actually DRAINS the steer queue (the simple/plan
# ReAct loop in chat_agent). Team / parallel / best-of-N runs never drain, so a
# steer there would queue forever — the /steer endpoint reports it unsupported
# instead of falsely claiming "queued". Marked by the message handler per run.
_STEERABLE: set[int] = set()


def set_steerable(session_id: int, on: bool) -> None:
    """Mark whether ``session_id``'s current run can consume steer messages."""
    if session_id is None:
        return
    with _LOCK:
        if on:
            _STEERABLE.add(session_id)
        else:
            _STEERABLE.discard(session_id)


def is_steerable(session_id: int) -> bool:
    """True if the session's in-flight run drains the steer queue."""
    if session_id is None:
        return False
    with _LOCK:
        return session_id in _STEERABLE


def push(session_id: int, text: str, *, require_steerable: bool = False) -> bool:
    """Queue a steer message for ``session_id``. Empty/blank text is a no-op.

    Returns True if the message was queued.

    When ``require_steerable`` is True the steerability check and the enqueue
    happen under the SAME lock acquisition — an atomic test-and-set. This closes
    the /steer TOCTOU (CC3): a separate ``is_steerable()`` then ``push()`` could
    let a steer be queued the instant a run-end ``clear()`` flips the session
    un-steerable, leaking a stale steer into the next turn. The endpoint now
    relies on this return value instead of a pre-check.
    """
    if session_id is None:
        return False
    if text is None:
        return False
    text = str(text).strip()
    if not text:
        return False
    with _LOCK:
        if require_steerable and session_id not in _STEERABLE:
            return False
        _QUEUES.setdefault(session_id, []).append(text)
    return True


def drain(session_id: int) -> list[str]:
    """Return + remove all pending steer messages for ``session_id`` (FIFO).

    Returns an empty list when nothing is pending.
    """
    if session_id is None:
        return []
    with _LOCK:
        msgs = _QUEUES.pop(session_id, None)
    return list(msgs) if msgs else []


def pending(session_id: int) -> bool:
    """True if there are queued steer messages for ``session_id``."""
    if session_id is None:
        return False
    with _LOCK:
        return bool(_QUEUES.get(session_id))


def clear(session_id: int) -> None:
    """Drop any queued steer messages for ``session_id`` (no leak across turns)."""
    if session_id is None:
        return
    with _LOCK:
        _QUEUES.pop(session_id, None)
        _STEERABLE.discard(session_id)
