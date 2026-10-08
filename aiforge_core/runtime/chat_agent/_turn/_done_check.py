"""When a turn is about to end, one short model call asks: is the request done?

The wording rules in ``_finish`` catch an answer that only announces work
("Let me fix it."), but every new phrasing needs a new rule, and a model that
ends on a plan, on "here is what I found so far" or on half the list passes
them. So the reply the turn would end on is read once against the user's
request by the model itself, with one question and a one-word answer:

* ``DONE`` — the reply reports the result, or answers the question.
* ``NEEDS_USER`` — it asks the user something, or names a blocker only the
  user can remove.
* ``UNFINISHED`` — a plan, an announcement, or progress with work left that
  the agent could do itself. The turn is sent back to carry on (bounded).

Anything else — an unclear verdict, a failed or slow call, a model that is
down, Stop — ends the turn as before: the check never holds an answer back on
its own doubt. Not in plan / read-only / builder runs, nor in work-producing
runs (they have their own finish rules); not for a one-word reply to a harness
check ("SAME"), nor after the running-job note said the model may answer
while a command runs. It costs one small request per turn end.

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
_CALLS_PER_TURN = 6
#: How often in a row one turn is sent back by the verdict.
_SEND_BACKS = 2

_SYSTEM = (
    "You check whether an AI coding agent may end its turn. You are given the "
    "user's request, the tools the agent ran in this turn, and the reply it "
    "wants to end the turn on. Answer with ONE word:\n"
    "DONE — the reply reports the finished result, or fully answers a question "
    "that asked for no work.\n"
    "NEEDS_USER — the reply asks the user something, or reports a blocker only "
    "the user can remove (missing access, a decision, an approval).\n"
    "UNFINISHED — the reply is a plan, an announcement of what it will do "
    "next, or a progress report with work left that the agent could do itself "
    "right now (errors still to fix, steps not run, a command it wants to "
    "retry).\n"
    "Judge from the reply against the request. A request to keep going until "
    "something works is not done while the reply says it does not work yet. "
    "When you cannot tell, answer DONE.")

_VERDICT_RE = re.compile(r"\b(UNFINISHED|NEEDS[_ ]USER|DONE)\b")

NUDGE = (
    "[harness — not the user] A check of your reply against the user's "
    "request says the request is not finished: the reply plans, announces or "
    "reports progress, and work is left that you can do yourself. Carry on "
    "now — take the next action. End the turn only when the request is done "
    "(say what you did and what the result was), or when you need the user "
    "(say exactly what you need).")


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_DONE_CHECK", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _timeout_s() -> int:
    try:
        return max(5, int(os.environ.get("AIFORGE_CHAT_DONE_CHECK_TIMEOUT_S", "60")))
    except ValueError:
        return 60


def _applies(st, builder, strict_finish) -> bool:
    return bool(enabled() and getattr(st, "session_id", None) is not None
                and not builder and not strict_finish
                and not getattr(st, "plan_mode", False)
                and not getattr(st, "readonly_mode", False)
                # That note allowed an answer while a command still runs.
                and not getattr(st, "running_job_nudged", False)
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


def _ran(st) -> str:
    counts = getattr(st, "action_counts", None) or {}
    names = list(dict.fromkeys(str(k).split("|", 1)[0] for k, v in counts.items() if v))
    return ", ".join(names[-12:]) or "(none)"


def verdict(role: str, goal: str, reply: str, ran: str, later: str = "",
            session_id=None) -> str:
    """``"done"`` / ``"needs_user"`` / ``"unfinished"``, or ``""`` when the
    model gave no clear word, the call failed or took too long, or the user
    pressed Stop. ``later``: what the user typed after the request."""
    ask = (f"USER'S REQUEST:\n{goal[:2000]}\n\n"
           + (f"LATER MESSAGES FROM THE USER (they override the request):\n"
              f"{later[:1500]}\n\n" if later else "")
           + f"TOOLS RUN THIS TURN: {ran}\n\n"
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
    # A reply that ends on a question is waiting for the user: nothing to ask.
    if (not text or not goal or text.endswith("?") or _to_the_harness(text)
            or not _applies(st, builder, strict_finish)):
        return None
    from ._finish import _budget_left
    # No send-back left: the verdict could not be used, so it is not asked for.
    if not _budget_left(st, "done_check_nudges", _SEND_BACKS, take=False):
        return None
    st.done_check_calls = getattr(st, "done_check_calls", 0) + 1
    yield {"type": "thought", "role": "system",
           "text": "⧗ checking the reply against the request…"}
    later = "\n".join(f"- {str(s).strip()}" for s in (getattr(st, "steers", None) or [])[-5:])
    said = verdict(str(getattr(st, "role", "") or "chat"), goal, text, _ran(st),
                   later=later, session_id=getattr(st, "session_id", None))
    if said != "unfinished" or not _budget_left(st, "done_check_nudges", _SEND_BACKS):
        return None
    yield {"type": "thought", "text": text}
    yield {"type": "thought", "role": "system",
           "text": "▶ the check says the request is not finished — continuing"}
    st.convo.append({"role": "user", "content": NUDGE})
    return "continue"


__all__ = ["NUDGE", "enabled", "gate", "verdict"]
