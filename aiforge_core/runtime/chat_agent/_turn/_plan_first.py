"""Before the first change of a turn, the agent says what it is about to do.

Live: asked to "make sure the code matches the Confluence page", the agent
rewrote the page; asked to "check what needs to be done", it started editing.
The user saw neither coming, because a native tool call carries no prose: the
first thing on screen was the edit.

So the first call of a turn that CHANGES something (a file write, a write to an
external system, a saved runbook) has to come with a short statement for the
user: what the agent understood, what it will change and what it will leave
alone. The statement is shown as a message of its own, and the work goes on —
the user corrects it by typing (a steer), nothing waits for a go-ahead.

* The model already said it next to the call: that text is shown, no extra step.
* It said nothing: the call is held once and the model is asked for the
  statement, then repeats its call. A request that only asks to check or
  compare, or that does not say which side should change, is to be answered,
  not acted on — the model reads that in the same note and decides.
* Reads and commands are never held. Plan / analyze / builder and
  work-producing runs (the pipeline Doer, subtasks, jobs) are not asked.

``AIFORGE_CHAT_SAY_PLAN=0`` turns it off.
"""
from __future__ import annotations

import os

#: A narration shorter than this is "ok" / "editing now", not a statement.
_MIN_CHARS = 60

#: The line a statement ends with when the changes are to be made now.
PROCEED = "PROCEED"

ASK = (
    "[harness — not the user] This call was NOT run. It is the first change "
    "of this turn, and the user has not been told what you are about to do.\n"
    "Reply now with TEXT ONLY — no tool call — addressed to the user.\n"
    "First read the user's last message again. If it asks you to check, "
    "review, compare, list or explain something, it did not ask for a change: "
    "give that answer (what you found, what would need to be done) and change "
    "nothing. If it does not say which side should change, ask. (\"Make the "
    "code match the page\" changes the code, not the page.)\n"
    "Only if the message asks for the change itself: say in 2 to 5 short "
    "lines what you understood, what you will change — name the files, or "
    "the page / ticket / system — what you will leave untouched, and the "
    f"steps; then end with a line holding the single word {PROCEED}.")

GO = ("[harness — not the user] The user has been shown what you wrote. Carry "
      "it out now, exactly as stated; a correction from the user may arrive "
      "while you work.")


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_SAY_PLAN", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def changes_something(name: str, args) -> bool:
    """A file write, a write to an external system, or a saved runbook."""
    try:
        from aiforge_core.runtime.tools import tool_policy
        from aiforge_core.runtime.tools.mutating import writes_files
        return (writes_files(name, args if isinstance(args, dict) else {})
                or name in tool_policy._DEFAULT_ASK
                or name in tool_policy._SCRIPT_TOOLS)
    except Exception:  # noqa: BLE001 — unsure: do not hold the call
        return False


def _applies(st) -> bool:
    return bool(enabled() and getattr(st, "session_id", None) is not None
                and not getattr(st, "readonly_mode", False)
                and not getattr(st, "builder", "")
                and not getattr(st, "strict_finish", False)
                and not _already_agreed(st))


def _already_agreed(st) -> bool:
    """The user's message is a go-ahead ("yes continue"): what is about to be
    done was said in the turn before. Saves the step; decides nothing else."""
    try:
        from . import _goahead
        return bool(_goahead.is_go_ahead(getattr(st, "goal", "") or ""))
    except Exception:  # noqa: BLE001
        return False


def gate(st, step, name, args):
    """Run before a tool call. Returns ``"continue"`` when the call is held for
    the statement, else None (the call goes on)."""
    if getattr(st, "plan_said", False) or not _applies(st) \
            or not changes_something(name, args):
        return None
    said = (step.get("thought") or "").strip()
    if len(said) >= _MIN_CHARS:
        st.plan_said = True
        yield {"type": "message", "supplementary": True, "role": "plan",
               "text": said}
        return None
    if getattr(st, "plan_asked_once", False):   # asked once: do not loop on it
        st.plan_said = True
        # Not waiting for the statement any more: a later FINAL is the answer.
        st.plan_pending = False
        return None
    st.plan_asked_once = True
    st.plan_pending = True
    yield {"type": "thought", "role": "system",
           "text": "↻ first change of this turn — asking the agent to say what "
                   "it is about to do…"}
    st.convo.append({"role": "user", "content": ASK})
    return "continue"


def on_text(st, step):
    """Run on a text reply (a FINAL). After :data:`ASK`, a reply that ends
    with PROCEED is the statement: it is shown and the work goes on
    (``"continue"``). Any other reply is the model's answer — it chose not to
    change anything — and goes to the user as the result (None)."""
    if not getattr(st, "plan_pending", False):
        return None
    st.plan_pending = False
    st.plan_said = True
    text = (step.get("text") or "").strip()
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[-1].strip().strip("*_`.").upper() != PROCEED:
        # Its own decision, taken with the question in front of it: the
        # zero-edit check would only ask the same thing again.
        st.zero_edit_checked = True
        st.zero_edit_answer = step.get("text") or ""
        return None
    said = "\n".join(lines[:-1]).strip()
    if said:
        yield {"type": "message", "supplementary": True, "role": "plan",
               "text": said}
    st.convo.append({"role": "assistant", "content": said or PROCEED})
    st.convo.append({"role": "user", "content": GO})
    return "continue"


__all__ = ["ASK", "GO", "PROCEED", "changes_something", "enabled", "gate",
           "on_text"]
