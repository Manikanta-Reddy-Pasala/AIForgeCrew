"""The response to a stuck run, written ONCE.

Every guard (same action, same output, idle replies, narration, no progress)
ends the same way, whatever it detected:

    recap nudge (``may_recover``, spent per stall)        -- some guards only
      -> change of approach (``escalate``: condense / restart, tiered)
      -> the wrap-up, then ``give_up_message`` and ``done``

and, with ``AIFORGE_CHAT_PAUSE_ON_STUCK=1``, the old pause-and-ask instead of
the last two. The guards decide WHEN a run is stuck and say what they saw
(``why``); what happens next is only in this module.

All three helpers are generators and return ``"continue"`` (carry on) or
``"return"`` (the turn is over: ``done`` was already yielded).
"""
from __future__ import annotations

from typing import NamedTuple

from aiforge_core.runtime.stuck_policy import Policy

from .._escalate import escalate, give_up_message


class Pause(NamedTuple):
    """The legacy pause-and-ask: what the user is told, and an optional
    status line yielded just before it."""
    text: str
    thought: str | None = None


def nudge(st, thought: str, text: str, *, reply: str | None = None):
    """Send one status line and one nudge to the model. ``reply`` is the
    model's own turn that provoked it: it goes in first, or two user turns in
    a row would break some providers."""
    yield {"type": "thought", "role": "system", "text": thought}
    if reply is not None:
        st.convo.append({"role": "assistant", "content": reply})
    st.convo.append({"role": "user", "content": text})


def change_approach(st, why: str, *, reply: str | None = None, prepare=None):
    """Change approach (``escalate``); once the escalations are spent, end the
    turn with the give-up message. ``prepare`` runs just before, ``reply`` is
    appended as the model's turn first (see :func:`nudge`)."""
    if prepare is not None:
        prepare()
    if reply is not None:
        st.convo.append({"role": "assistant", "content": reply})
    result = yield from escalate(st, why)
    if result == "continue":
        return "continue"
    yield {"type": "message", "text": give_up_message(st)}
    yield {"type": "done"}
    return "return"


def pause_and_ask(pause: Pause):
    if pause.thought:
        yield {"type": "thought", "role": "system", "text": pause.thought}
    yield {"type": "message", "awaiting_input": True, "text": pause.text}
    yield {"type": "done"}
    return "return"


def finish_stuck(st, why: str, pause: Pause, *, reply: str | None = None,
                 prepare=None):
    """The tail every guard shares: change approach, or pause and ask when the
    operator asked for the old behaviour."""
    if Policy.load().pause_on_stuck:
        return (yield from pause_and_ask(pause))
    return (yield from change_approach(st, why, reply=reply, prepare=prepare))
