"""When a turn is about to end, one short model call asks: is the request done?

The wording rules in ``_finish`` catch an answer that only announces work
("Let me fix it."), but every new phrasing needs a new rule, and a model that
ends on a plan, on "here is what I found so far" or on half the list passes
them. So the reply the turn would end on is read once against the user's
request by the model itself, with one question and a one-word answer:

* ``DONE`` — the reply reports the result, or answers the question.
* ``NEEDS_USER`` — it cannot go on without the user: a real question, or a
  blocker only the user can remove.
* ``UNFINISHED`` — a plan, an announcement, progress with work left that the
  agent could do itself, a question that only asks leave to do what was
  asked for ("want me to fix the rest?"), or "fixed" with no check run after
  the last change. The turn is sent back to carry on (bounded).

The checker reads what the harness measured, not only the reply: the turn's
last actions with their outcome (✓ worked, ✗ failed), the commands that
failed after the last file change and never passed again, the open items of
the task board, what the user typed later, and — for a bare "continue" — the
request before it. A reply that ends with a question, and a question asked
with the ask tool, are read the same way.

Anything else — an unclear verdict, a failed or slow call, a model that is
down, Stop — ends the turn as before: the check never holds an answer back on
its own doubt. Not in plan / read-only / builder runs, nor in work-producing
runs (they have their own finish rules); not for a one-word reply to a harness
check ("SAME"), nor while a command the running-job note spoke of is still
running, nor once the loop guard has told the model to wrap up. A reply the
check calls unfinished when its send-backs are spent goes out with a line
saying so (:func:`unfinished_note`). It costs one small request per turn
end.

``AIFORGE_CHAT_DONE_CHECK=0`` turns it off.
"""
from __future__ import annotations

import contextvars
import os
import re
import threading
import time

from ._shared import _log

#: How often one turn asks (each later answer of the turn is checked again).
_CALLS_PER_TURN = 30
#: How often in a row one turn is sent back by the verdict.
_SEND_BACKS = 3

_SYSTEM = (
    "You check whether an AI coding agent may end its turn. You are given the "
    "user's request, what the harness measured in this turn (the latest "
    "actions with their outcome: ✓ worked, ✗ failed, … still running), and "
    "the reply the agent wants to end the turn on. Answer with ONE word:\n"
    "DONE — the reply reports the finished result and the measured actions "
    "bear it out, or it fully answers a question that asked for no work.\n"
    "NEEDS_USER — the agent cannot go on without the user: it asks something "
    "only the user can answer (a missing value, a real choice between "
    "options), or reports a blocker only the user can remove (missing "
    "access, an approval).\n"
    "UNFINISHED — any of these: the reply is a plan or an announcement of "
    "what it will do next; it reports progress with work left that the agent "
    "could do itself right now (errors still to fix, steps not run, a "
    "command to retry); it asks leave to do what the request already asks "
    "for (\"shall I continue?\", \"want me to fix the rest?\"); it says "
    "something is fixed or works, but the measured actions show no check, "
    "test or run of it after the last change, or the last such run failed; "
    "items of the task board are still open.\n"
    "A request to keep going until something works is not done while the "
    "last run of it failed. Trust the measured actions over the reply's "
    "wording. Only when the request asked for no work and nothing was run, "
    "answer DONE on the reply alone.")

_VERDICT_RE = re.compile(r"\b(UNFINISHED|NEEDS[_ ]USER|DONE)\b")

NUDGE = (
    "[harness — not the user] A check of your reply against the user's "
    "request says the request is not finished: the reply plans, announces or "
    "reports progress, and work is left that you can do yourself. Carry on "
    "now — take the next action. End the turn only when the request is done "
    "(say what you did and what the result was), or when you need the user "
    "(say exactly what you need). Do not ask for leave to do what the request "
    "already asks for. If you changed something, run the check, test or "
    "command that shows it works now, and fix what it reports.")


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_DONE_CHECK", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _timeout_s() -> int:
    try:
        return max(5, int(os.environ.get("AIFORGE_CHAT_DONE_CHECK_TIMEOUT_S", "60")))
    except ValueError:
        return 60


def job_note_holds(st) -> bool:
    """The running-job note allowed an answer while a command runs — for as
    long as one still does, not for the rest of the turn."""
    if not getattr(st, "running_job_nudged", False):
        return False
    try:
        from aiforge_core.runtime import cmd_jobs
        return bool(cmd_jobs.turn_running())
    except Exception:  # noqa: BLE001
        return True


def _applies(st, builder, strict_finish) -> bool:
    return bool(enabled() and getattr(st, "session_id", None) is not None
                and not builder and not strict_finish
                and not getattr(st, "plan_mode", False)
                and not getattr(st, "readonly_mode", False)
                and not job_note_holds(st)
                # The loop guard asked for "what is done, what remains".
                and not getattr(st, "wrapping_up", False)
                and getattr(st, "done_check_calls", 0) < _CALLS_PER_TURN)


def _to_the_harness(text: str) -> bool:
    """A reply to one of the harness's own checks ("SAME"), not an answer."""
    try:
        from .._guards.zero_edit import _says_same
        return bool(_says_same(text))
    except Exception:  # noqa: BLE001
        return False


def _stopped(session_id) -> bool:
    try:
        from aiforge_core.runtime import chat_cancel
        return bool(chat_cancel.is_cancelled(int(session_id)))
    except Exception:  # noqa: BLE001
        return False


#: How many of the turn's latest actions the checker reads.
_ACTIONS_SHOWN = 12


def _measured(st) -> str:
    """What the harness measured in this turn, for the checker: the latest
    actions with their outcome, the commands that failed after the last file
    change and never passed again, and the task board's open items."""
    lines: list = []
    try:
        from aiforge_core.runtime import action_log
        steps = action_log.live_steps(st.session_id)
        items = action_log.entries(steps)
        hidden = max(0, len(items) - _ACTIONS_SHOWN)
        if hidden:
            failed = sum(1 for e in items[:hidden] if e.get("ok") is False and not e.get("fixed"))
            lines.append(f"({hidden} earlier actions not shown, {failed} of them failed)")
        lines += [action_log.text_of(e) for e in items[-_ACTIONS_SHOWN:]]
        from .._guards.turn_facts import unpassed_failures
        for cmd, head in unpassed_failures(steps):
            lines.append(f"FAILED AFTER THE LAST FILE CHANGE, NOT PASSED SINCE: {cmd} — {head}")
    except Exception:  # noqa: BLE001 — the facts never block the check
        pass
    if not lines:
        counts = getattr(st, "action_counts", None) or {}
        names = list(dict.fromkeys(str(k).split("|", 1)[0] for k, v in counts.items() if v))
        lines.append("tools run: " + (", ".join(names[-12:]) or "(none)"))
    try:
        from ._tasks import open_items
        board = getattr(st, "board", None) or {}
        left = [f"{s}: {board[s].get('title') or ''}".strip() for s in open_items(board)]
        if left:
            lines.append("TASK BOARD, STILL OPEN: " + "; ".join(left[:8]))
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)[:4000]


#: A request this short says nothing by itself ("continue", "yes go on").
_THIN_GOAL_CHARS = 40


def _earlier_request(st, goal: str) -> str:
    """For a bare "continue": the user's message before it."""
    if len(goal) >= _THIN_GOAL_CHARS:
        return ""
    try:
        from .._context import _text_of
        from .._context._compaction import _is_harness_note
        from ._convo import strip_turn_note
        mine = []
        for m in getattr(st, "convo", None) or []:
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            text = strip_turn_note(_text_of(m)).strip()
            if (text and not text.startswith(("OBSERVATION", "[", "<<", "("))
                    and not _is_harness_note(text)):
                mine.append(text)
        earlier = [t for t in mine if " ".join(t.split()) != " ".join(goal.split())]
        return earlier[-1][:1500] if earlier else ""
    except Exception:  # noqa: BLE001
        return ""


#: Opens the line (so a copy of it in a later answer can be taken out).
UNFINISHED_HEAD = "Harness check:"


def unfinished_note(st) -> str:
    """The line an answer ends with when the check still called it
    unfinished and could not send it back again ("" otherwise)."""
    if not getattr(st, "done_check_unfinished", False):
        return ""
    return (f"\n\n_{UNFINISHED_HEAD} this answer does not look finished "
            "against your request — work is left, or the fix was not run "
            "after the last change. Say **continue** and I carry on._")


def verdict(role: str, goal: str, reply: str, ran: str, later: str = "",
            session_id=None, earlier: str = "") -> str:
    """``"done"`` / ``"needs_user"`` / ``"unfinished"``, or ``""`` when the
    model gave no clear word, the call failed or took too long, or the user
    pressed Stop. ``ran``: what the harness measured (:func:`_measured`);
    ``later``: what the user typed after the request; ``earlier``: the
    request before a bare "continue"."""
    ask = ((f"THE REQUEST BEFORE IT (the work the user means):\n{earlier}\n\n"
            if earlier else "")
           + f"USER'S REQUEST:\n{goal[:2000]}\n\n"
           + (f"LATER MESSAGES FROM THE USER (they override the request):\n"
              f"{later[:1500]}\n\n" if later else "")
           + f"MEASURED BY THE HARNESS IN THIS TURN:\n{ran}\n\n"
           f"REPLY THE AGENT WANTS TO END ON:\n{reply[:3000]}\n\n"
           "One word, in English, exactly as written here: DONE, NEEDS_USER "
           "or UNFINISHED.")
    box: dict = {}

    def call():
        try:
            from aiforge_core.llm import client, model_wait
            # Optional: a model that is down ends the check, not the answer.
            with model_wait.optional():
                box["out"] = client.complete(role, [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": ask},
                ], max_tokens=200, temperature=0, timeout_s=_timeout_s())
        except Exception as exc:  # noqa: BLE001 — the check never blocks an answer
            _log.debug("done check failed: %s", exc)

    # Off the turn's thread, so Stop and the time limit end the wait (the
    # request itself is left to run out); the turn's context rides along, so
    # the call is counted with the turn's other requests.
    worker = threading.Thread(target=contextvars.copy_context().run, args=(call,),
                              daemon=True, name="done-check")
    worker.start()
    until = time.monotonic() + _timeout_s() + 5
    while worker.is_alive() and time.monotonic() < until:
        if session_id is not None and _stopped(session_id):
            return ""
        worker.join(0.2)
    words = _VERDICT_RE.findall(str(box.get("out") or "").upper())
    # (the last one: a model that reasons first names the verdict at the end)
    return words[-1].replace(" ", "_").lower() if words else ""


def gate(st, step, builder=None, strict_finish=False):
    """Run on the reply a turn would end on. Returns ``"continue"`` when the
    check says the request is unfinished (the model is sent back), else None."""
    text = str(step.get("text") or "").strip()
    goal = str(getattr(st, "goal", "") or "").strip()
    # What an earlier verdict said was about an earlier reply.
    st.done_check_unfinished = False
    if (not text or not goal or _to_the_harness(text)
            or not _applies(st, builder, strict_finish)):
        return None
    from ._finish import _budget_left
    st.done_check_calls = getattr(st, "done_check_calls", 0) + 1
    yield {"type": "thought", "role": "system",
           "text": "⧗ checking the reply against the request…"}
    later = "\n".join(f"- {str(s).strip()}" for s in (getattr(st, "steers", None) or [])[-5:])
    said = verdict(str(getattr(st, "role", "") or "chat"), goal, text, _measured(st),
                   later=later, session_id=getattr(st, "session_id", None),
                   earlier=_earlier_request(st, goal))
    st.done_check_unfinished = said == "unfinished"
    if said != "unfinished" or not _budget_left(st, "done_check_nudges", _SEND_BACKS):
        return None
    st.done_check_unfinished = False          # sent back: the next reply is judged anew
    yield {"type": "thought", "text": text}
    yield {"type": "thought", "role": "system",
           "text": "▶ the check says the request is not finished — continuing"}
    st.convo.append({"role": "user", "content": NUDGE})
    return "continue"


__all__ = ["NUDGE", "enabled", "gate", "job_note_holds", "unfinished_note", "verdict"]
