"""Notice Stop or a newly typed message during a blocking chat-agent wait.

A command, a watch, and the pre-turn model calls used to run until their own
timer ended. Stop had already set the cancel token, and the typed message was
already in the steer queue, but nothing looked until the wait returned — so
Stop looked dead and the message sat unread for the whole command.
"""
from __future__ import annotations

import contextlib

# Shown to the model when a tool bails out because the user typed something.
# The steer text itself is folded in by the turn loop; this only says why the
# tool stopped early, so the model decides whether the action is still needed.
STEER_ERROR = (
    "paused — the user sent a new message while this was running. "
    "That message is their latest instruction. Decide whether this "
    "action is still needed, or drop it and do the new task. Do not "
    "resume the wait unless the new message still needs it."
)

# wait_future returns this when Stop fired, distinct from any real result.
STOPPED = object()


def reason(session_id) -> "str | None":
    """``"stop"`` if the user pressed Stop, ``"steer"`` if they typed a
    message, else None. Stop wins when both are set."""
    if session_id is None:
        return None
    from aiforge_core.runtime import chat_cancel, chat_interject
    if chat_cancel.is_cancelled(session_id):
        return "stop"
    try:
        if chat_interject.pending(session_id):
            return "steer"
    except Exception:  # noqa: BLE001 — a steer probe must not wedge the wait
        return None
    return None


def pause(seconds: float, session_id, slice_s: float = 0.2) -> "str | None":
    """Sleep up to ``seconds``, returning as soon as Stop or a new message
    arrives. None means the full wait elapsed with neither."""
    import time
    if seconds <= 0:
        return reason(session_id)
    waited = 0.0
    while waited < seconds:
        why = reason(session_id)
        if why:
            return why
        step = min(slice_s, seconds - waited)
        time.sleep(step)
        waited += step
    return reason(session_id)


def steered(**extra) -> dict:
    """Tool result for a wait/command cut short by a new user message."""
    out = {"ok": False, "steered": True, "error": STEER_ERROR}
    out.update(extra)
    return out


def wait_future(fut, timeout: float, session_id):
    """``fut.result``, but Stop returns :data:`STOPPED` instead of sitting
    out the timeout. A timeout still raises ``TimeoutError``."""
    import time
    from concurrent.futures import TimeoutError as FutTimeout
    end = time.monotonic() + max(0.0, float(timeout))
    while True:
        if reason(session_id) == "stop":
            return STOPPED
        left = end - time.monotonic()
        if left <= 0:
            return fut.result(timeout=0)
        try:
            return fut.result(timeout=min(0.2, left))
        except FutTimeout:
            continue


@contextlib.contextmanager
def bind_llm_cancel(session_id):
    """Point this thread's LLM HTTP call at the session Stop event, so
    ``complete()`` aborts when Stop is pressed instead of running out its
    timeout. No active run → the call is unchanged."""
    from aiforge_core.llm import client as llm
    from aiforge_core.runtime import chat_cancel
    tok = chat_cancel.get(session_id) if session_id is not None else None
    if tok is None:
        yield
        return
    llm.set_cancel_event(tok.event)
    try:
        yield
    finally:
        llm.set_cancel_event(None)
