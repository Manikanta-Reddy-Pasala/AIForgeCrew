"""A question nobody answers does not stop the work.

Owner, 11-16: "If user is not responding let model take a call — not a
deletion one. I see so many suggested stuff and waiting for user input: let
it continue without user input."

When the agent (or the goal tracker) asks the user something, the question is
shown and the run waits up to AIFORGE_CHAT_ASK_WAIT_S (default 90 s) for a
reply — a message typed while a run is going already reaches it. No reply:
the model is told to make the call itself (the option it would recommend),
say what it chose in one line, and go on. A question about deleting,
dropping, resetting or overwriting anything still stops and waits for the
user. Unattended runs (no chat session) decide at once. 0 = the old
behaviour (every question ends the turn).
"""
from __future__ import annotations

import os
import re
import time

_WAIT_S = 90
#: Auto-decisions per turn; after that a question ends the turn as before.
_MAX_PER_TURN = 5
_DESTRUCTIVE = re.compile(
    r"\b(?:delet\w*|remov\w*|drop\w*|wip\w*|purg\w*|eras\w*|destroy\w*|truncat\w*|"
    r"discard\w*|reset\w*|overwrit\w*|force[- ]push\w*|rm\s+-|clean\s*up\s+(?:old|the)|"
    r"uninstall\w*|revoke\w*|deactivat\w*|cancel\s+(?:the\s+)?(?:order|subscription|invoice))\b",
    re.I)

DECIDE_NOTE = (
    "[harness — not the user] The user did not answer within {secs}s. Do not wait: "
    "make the call yourself — take the option you would recommend (the safest one "
    "that still does what was asked), say in one line which you chose and why, then "
    "continue the task. Do not ask this again.")


def wait_s() -> int:
    try:
        return max(0, int(os.environ.get("AIFORGE_CHAT_ASK_WAIT_S", str(_WAIT_S))))
    except ValueError:
        return _WAIT_S


def destructive(question: str) -> bool:
    return bool(_DESTRUCTIVE.search(question or ""))


def may_decide(st, question: str) -> bool:
    """This question may be decided by the model when unanswered."""
    if os.environ.get("AIFORGE_CHAT_ASK_WAIT_S", "").strip() == "0":
        return False
    if getattr(st, "plan_mode", False) or destructive(question):
        return False
    return int(getattr(st, "auto_decided", 0) or 0) < _MAX_PER_TURN


def wait_for_answer(session_id, secs: int, *, poll: float = 1.0, _sleep=time.sleep,
                    _now=time.monotonic) -> str:
    """``"answered"`` when the user typed something, ``"cancelled"`` when the run
    was stopped, else ``"timeout"``. No session (an unattended run): at once."""
    if session_id is None or secs <= 0:
        return "timeout"
    from aiforge_core.runtime import chat_cancel, chat_interject
    end = _now() + secs
    while _now() < end:
        try:
            if chat_interject.pending(session_id):
                return "answered"
            if chat_cancel.is_cancelled(session_id):
                return "cancelled"
        except Exception:  # noqa: BLE001
            return "timeout"
        _sleep(poll)
    return "timeout"


def ask_and_wait(st, session_id, question: str):
    """Show ``question``, wait for a reply, and on silence tell the model to
    decide. Returns ``"continue"`` (the run goes on — the reply, if any, is
    picked up by the steering drain at the next step) or ``"cancelled"``."""
    secs = wait_s() if session_id is not None else 0
    st.auto_decided = int(getattr(st, "auto_decided", 0) or 0) + 1
    yield {"type": "message", "role": "assistant", "text": question}
    if secs:
        yield {"type": "thought", "role": "system",
               "text": f"⏳ waiting up to {secs}s for your answer — after that I decide and continue"}
    got = wait_for_answer(session_id, secs)
    if got == "cancelled":
        return "cancelled"
    st.convo.append({"role": "assistant", "content": question})
    if got == "answered":
        st.convo.append({"role": "user", "content":
                         "[harness — not the user] The user's answer to your question follows."})
        return "continue"
    st.convo.append({"role": "user", "content": DECIDE_NOTE.format(secs=secs)})
    yield {"type": "thought", "role": "system",
           "text": "▶ no answer — the agent decides and continues"}
    return "continue"


__all__ = ["DECIDE_NOTE", "ask_and_wait", "destructive", "may_decide", "wait_for_answer", "wait_s"]
