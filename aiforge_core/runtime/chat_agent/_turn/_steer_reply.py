"""A message typed while the agent works gets a reply the user can see.

Live: the user asked a question, or gave a direction, while a run was going.
The model read it at its next step and went on with a tool call. A native tool
call carries no prose, so nothing answered the user; when the model did write
a line, it was one more row in the steps list, which folds away when the turn
ends. Either way the user saw no answer.

So the first thing the model says after reading a mid-run message is shown as
a reply of its own (``chat_steer.reply_event``), outside the steps list.

* The model said something next to its next call: that text is the reply.
* It said nothing: the call is held once and the model is asked for a short
  reply, then carries on. One extra model step per message, never a loop. A
  model that answers the ask with another call (the lookup its answer needs)
  is not held again: the next words it writes are the reply.
* Its next reply is the turn's answer: that answer is the reply; nothing more.
* Work-producing runs (the pipeline Doer, subtasks, builders) are not asked.

``AIFORGE_CHAT_STEER_REPLY=0`` turns it off.
"""
from __future__ import annotations

import os

#: Shorter than this is "ok" / "on it", not a reply to what was asked.
_MIN_CHARS = 20

ASK = (
    "[harness — not the user] This call was NOT run yet. The user's message "
    "above, sent while you were working, has had no reply.\n"
    "Reply now with TEXT ONLY — no tool call — addressed to the user, in one "
    "to four short sentences: answer what they asked, or say what you will do "
    "about what they said. It is shown to them at once. Do not sum up the "
    "whole task; you carry on with it right after. If you have to look "
    "something up before you can answer, make that call, and write the reply "
    "as text next to your call after it.")

GO = ("[harness — not the user] The user has been shown your reply. Now carry "
      "on with the task you were on, changed as their message asks. If their "
      "message ended the task, or nothing is left to do, give your final "
      "answer.")


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_STEER_REPLY", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _applies(st) -> bool:
    return bool(enabled() and getattr(st, "session_id", None) is not None
                and not getattr(st, "builder", "")
                and not getattr(st, "strict_finish", False))


def _settle(st) -> list:
    due = list(getattr(st, "reply_due", None) or [])
    st.reply_due, st.reply_pending, st.reply_asked = [], False, False
    return due


def _event(st, text: str) -> dict:
    from aiforge_core.runtime import chat_steer
    return chat_steer.reply_event(_settle(st), text)


def on_drain(st, texts) -> None:
    """Messages the model is about to read: each is owed a reply."""
    texts = [t for t in (texts or []) if str(t).strip()]
    if texts and _applies(st):
        st.reply_due = [*(getattr(st, "reply_due", None) or []), *texts]
        st.reply_asked = False


def gate(st, step, name, args):
    """Run before a tool call. Returns ``"continue"`` when the call is held
    for the reply, else None (the call goes on)."""
    del name, args
    if not getattr(st, "reply_due", None) or not _applies(st):
        return None
    # A call, not the text that was asked for: a later FINAL is the turn's
    # answer, not the reply.
    st.reply_pending = False
    said = (step.get("thought") or "").strip()
    if len(said) >= _MIN_CHARS:
        yield _event(st, said)
        # The user has been told what comes next: the statement before a
        # first change is not asked for, or shown, a second time.
        st.plan_said = True
        return None
    if getattr(st, "plan_pending", False):
        # The statement before a first change is being asked for: one ask at
        # a time. What the model writes there is the reply (see on_text).
        return None
    if getattr(st, "reply_asked", False):
        # Asked once, and it made a call instead (often the lookup the answer
        # needs). Never held twice: the next words it writes are the reply.
        return None
    st.reply_asked = True
    st.reply_pending = True
    yield {"type": "thought", "role": "system",
           "text": "↻ your message has had no reply yet — asking the agent to "
                   "answer it…"}
    st.convo.append({"role": "user", "content": ASK})
    return "continue"


def on_text(st, step):
    """Run on a text reply (a FINAL). After :data:`ASK` the text is the reply:
    it is shown and the work goes on (``"continue"``). Without the ask, the
    text is the turn's answer, written after the message was read — it goes to
    the user as the result (None)."""
    text = (step.get("text") or "").strip()
    if not getattr(st, "reply_due", None):
        _keep_the_longer_answer(st, step, text)
        return None
    if not getattr(st, "reply_pending", False) or not text:
        said = _statement(st, text)
        if said:
            # The statement asked for before a first change: the work goes on
            # after it, so it is the only thing said about the message.
            yield _event(st, said)
        _settle(st)
        return None
    yield _event(st, text)
    st.reply_said, st.reply_said_at = text, _tool_calls(st)
    st.convo.append({"role": "user", "content": GO})
    return "continue"


def _tool_calls(st) -> int:
    try:
        return sum(int(v or 0) for v in
                   (getattr(st, "action_counts", None) or {}).values())
    except Exception:  # noqa: BLE001
        return 0


def _keep_the_longer_answer(st, step, text: str) -> None:
    """The reply WAS the whole answer (a plan, a summary) and, told to carry
    on, the model ended at once with a stub ("see above"). No tool ran in
    between, so the reply is the answer: it is what gets stored as the turn's
    result, and what the next turn reads."""
    said = getattr(st, "reply_said", "") or ""
    if not said:
        return
    st.reply_said = ""
    if text and len(text) < len(said) and _tool_calls(st) == getattr(
            st, "reply_said_at", -1):
        step["text"] = said


def _statement(st, text: str) -> str:
    """The plan-first statement in ``text`` (it ends with PROCEED), or "" —
    any other text is the turn's answer and is shown as that."""
    if not text or not getattr(st, "plan_pending", False):
        return ""
    from . import _plan_first
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[-1].strip().strip("*_`.").upper() != _plan_first.PROCEED:
        return ""
    return "\n".join(lines[:-1]).strip()


def on_narration(st, step):
    """A reply that says what comes next and runs nothing: its words are the
    reply."""
    st.reply_pending = False
    said = (step.get("thought") or step.get("text") or "").strip()
    if getattr(st, "reply_due", None) and _applies(st) \
            and len(said) >= _MIN_CHARS:
        yield _event(st, said)


def on_ask(st) -> None:
    """The turn ends on a question to the user: that is what they see."""
    if getattr(st, "reply_due", None):
        _settle(st)


__all__ = ["ASK", "GO", "enabled", "gate", "on_ask", "on_drain",
           "on_narration", "on_text"]
