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


def push(session_id: int, text: str) -> bool:
    """Queue a steer message for ``session_id``. Empty/blank text is a no-op.

    Returns True if the message was queued.
    """
    if session_id is None:
        return False
    if text is None:
        return False
    text = str(text).strip()
    if not text:
        return False
    with _LOCK:
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
