"""Before the first change of a big request, the lead is asked once whether
parts of it can go to doer agents.

Bench (attrs-inspect, click-completion, 11-08): ``run_doers`` was on the tool
list, the rule line was in the system prompt, and the model still did every
part itself. It reads one tool among twenty and keeps going. So the first
file-changing call of a big request (the spec audit's size test: a long
message or three parts on the board) is held once with a short note; the
model either splits the independent parts off with ``run_doers`` or repeats
its call and carries on alone. It decides, the note only asks.

It runs before the plan-first statement gate and asks the model to say what
it will do next to its call, so that gate shows that text instead of holding
the call a second time. Only file writes are held (a Jira or page write is
not split into doers).

Not asked: a doer itself, plan / analyze / builder / strict runs, the Plan
pipeline's Doer and scheduled jobs (work runs, see ``quiet``), a chat with
approvals on (doers would skip them), or too little turn time for doers.
``AIFORGE_CHAT_DOER_HINT=0`` turns it off; ``AIFORGE_DOERS=0`` also does.
On by default: same accuracy (shopkit 46/60 vs 44/60 alone). On one local
model server it is slower (90 min vs ~51 min: parallel calls slow each
other); on hardware that serves requests in parallel it is expected to be
faster.
"""
from __future__ import annotations

import contextvars
import os

#: A run doing its own prompt's work (the Plan pipeline's Doer): not asked.
_QUIET: "contextvars.ContextVar[bool]" = contextvars.ContextVar("doer_hint_quiet",
                                                               default=False)

HINT = (
    "[harness — not the user] This call was NOT run. This is a big request: "
    "before you change files, decide how to split it.\n"
    "If it has two or more parts that do not depend on each other and touch "
    "different files, hand those parts to run_doers in ONE call (each part: "
    "title, the full task text with names, rules and examples, and the files "
    "it may change). They run side by side; you then run the tests and finish. "
    "Keep linked work, and anything that shares a file, for yourself.\n"
    "If the parts are linked, repeat your call and carry on yourself.\n"
    "Either way, say in one or two lines next to your call what you will do "
    "now: the user sees that line.")


def quiet(gen):
    """Run ``gen`` (a chat agent run) with the hint off, each step inside the
    switch: for a work run such as the Plan pipeline's Doer."""
    while True:
        tok = _QUIET.set(True)
        try:
            ev = next(gen)
        except StopIteration:
            return
        finally:
            _QUIET.reset(tok)
        yield ev


def enabled() -> bool:
    off = ("0", "false", "no", "off")
    return (os.environ.get("AIFORGE_CHAT_DOER_HINT", "1").strip().lower() not in off
            and os.environ.get("AIFORGE_DOERS", "1").strip().lower() not in off)


def _applies(st) -> bool:
    if not enabled() or getattr(st, "doer_hinted", False) or _QUIET.get():
        return False
    if getattr(st, "session_id", None) is None or getattr(st, "readonly_mode", False) \
            or getattr(st, "plan_mode", False) or getattr(st, "builder", "") \
            or getattr(st, "strict_finish", False):
        return False
    try:
        from aiforge_core.runtime import chat_goals
        if chat_goals._OFF_RUN.get():          # a scheduled job's run
            return False
        from .._tools import _doers
        from ._spec_audit import _big
        if _doers.IN_DOER.get() or not _big(st):
            return False
        if _doers._budget_s() < 300 or _doers._approvals_on(st.session_id):
            return False
    except Exception:  # noqa: BLE001 — unsure: do not hold the call
        return False
    return True


def gate(st, step, name, args):
    """Run before a tool call. Returns ``"continue"`` when the call is held for
    the split decision, else None."""
    if name == "run_doers":
        st.doer_hinted = True
        return None
    try:
        from aiforge_core.runtime.tools.mutating import writes_files
        writes = writes_files(name, args if isinstance(args, dict) else {})
    except Exception:  # noqa: BLE001 — unsure: do not hold the call
        writes = False
    if not writes or not _applies(st):
        return None
    st.doer_hinted = True
    yield {"type": "thought", "role": "system",
           "text": "↻ big request — asking the agent whether parts can go to doers…"}
    st.convo.append({"role": "user", "content": HINT})
    return "continue"
