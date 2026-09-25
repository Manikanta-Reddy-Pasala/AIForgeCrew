"""Notice Stop or a newly typed message during a blocking chat-agent wait.

A command, a watch, and the pre-turn model calls used to run until their own
timer ended. Stop had already set the cancel token, and the typed message was
already in the steer queue, but nothing looked until the wait returned — so
Stop looked dead and the message sat unread for the whole command.
"""
from __future__ import annotations

import contextlib
import re

# The latest message asks to stop or replace the work already running.
# "also name it add_numbers" does not. The newest message wins.
_REPLACE_RE = re.compile(
    r"\b(stop|cancel|abort|drop|kill|halt|quit|forget|instead|"
    r"scratch|never\s*mind|do not|don't|dont|no longer|hold on|"
    r"wait no|actually no|switch)\b",
    re.IGNORECASE,
)

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

# A retry or outage wait ended because a new message arrived. The step
# loop drains it and calls the model again. A task that is already
# running is not cut off this way — see attention(only_replace=True).
STEERED = object()


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


def replaces_running_work(session_id) -> bool:
    """True when the newest queued message tells the agent to stop or
    replace the scheduler or task that is already running.

    An extra detail stays queued and is applied when that work returns.
    The work itself is not cut off to read it."""
    from aiforge_core.runtime import chat_interject
    try:
        texts = chat_interject.peek_texts(session_id)
    except Exception:  # noqa: BLE001
        return False
    if not texts:
        return False
    return _REPLACE_RE.search(texts[-1]) is not None


def attention(session_id, *, only_replace: bool = False) -> "str | None":
    """What a running wait should do about Stop or a new message.

    ``only_replace`` is for a scheduler or task already in progress: a
    message is noticed, and the work stops only when that message asks
    to stop or replace it. Stop still wins immediately."""
    why = reason(session_id)
    if why == "steer" and only_replace and not replaces_running_work(session_id):
        return None
    return why


def pause(seconds: float, session_id, slice_s: float = 0.2,
          *, only_replace: bool = False) -> "str | None":
    """Sleep up to ``seconds``, returning as soon as Stop arrives.

    A new message returns immediately too, unless ``only_replace`` is set:
    then an extra detail lets the wait finish, and only a message that
    stops or replaces the work cuts it short. None means the full wait
    elapsed with nothing to act on."""
    import time
    if seconds <= 0:
        return attention(session_id, only_replace=only_replace)
    waited = 0.0
    while waited < seconds:
        why = attention(session_id, only_replace=only_replace)
        if why:
            return why
        step = min(slice_s, seconds - waited)
        time.sleep(step)
        waited += step
    return attention(session_id, only_replace=only_replace)


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
