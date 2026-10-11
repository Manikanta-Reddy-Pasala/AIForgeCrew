"""The task board is written by the model, not by a sentence splitter.

The board used to come from ``_split_asks``: bullets, or sentences that start
like an order, cut at 160 characters, at most 8. A message that named two
repositories, asked for something "in between", or said one thing in passing
came out as "task 1, task 2, task 3" copied from the wording — work missed,
names lost, nothing to check an item against. And a message typed while the
agent works only reached the board if the working model chose to call
``plan_progress``.

Two short model calls fix both:

* :func:`plan_asks` — at the start of an act turn, the message (with the
  conversation before it, so "both repos" and "the same" resolve) becomes a
  refined task list: one self-contained item per piece of work, in the order
  it is done, each naming its repository / file / command exactly as the user
  wrote it and ending in how the agent shows it is done ("— check: …").
  ``[]`` for a question or a one-step request (no board, as before).
* :func:`revise` — when a message arrives mid-run, the board is brought in
  line with it by the harness: work added (where the user said it belongs),
  changed, dropped or to be redone. A question or an "ok" changes nothing.

A failed, slow or unreadable call falls back to the old behaviour (the
splitter at turn start, the "update your board" note on a steer); Stop ends
the wait. ``AIFORGE_CHAT_LLM_PLAN=0`` turns both off.
"""
from __future__ import annotations

import contextvars
import json
import os
import threading
import time

from ._shared import _log

#: Items one plan may hold; the board itself takes more as the run adds them.
_MAX_TASKS = 12
_MAX_TITLE = 300
#: Shorter than this, a message is "continue" / "yes" / "ok": nothing to plan.
_MIN_CHARS = 25

_RULES = (
    "How to write a task:\n"
    "- One task per piece of work, in the order the work should be done. "
    "Work the user mentions in passing (\"also…\", \"before that…\", "
    "\"in between…\", \"and then…\") is a task too, placed where the user "
    "said it belongs.\n"
    "- Refine, do not copy: each task is one concrete action the agent can "
    "do and check, readable on its own later without the message.\n"
    "- Keep every name exactly as the user wrote it: repositories, folders, "
    "files, branches, hosts, services, commands, numbers. When the work spans "
    "several repositories or places, each task names the one it is about.\n"
    "- Resolve \"the same\", \"both\", \"that file\", \"the other repo\" from "
    "the earlier conversation.\n"
    "- End each task with \" — check: \" and how the agent shows it is done "
    "(a test or command that passes, a file that exists, the answer given).\n"
    "- A rule about how to work (\"don't push\", \"don't edit the tests\") "
    "is not a task.\n"
    "- Do not add work the user did not ask for. Split a big piece of work "
    "into its natural steps only when it really has several.\n")

_PLAN_SYSTEM = (
    "You write the task list an AI coding agent works through for the "
    "user's newest message. The user watches the list; the agent is held to "
    "it before it may finish.\n" + _RULES
    + "- A plain question, a greeting, a status ask, or a request that is a "
    "single small step: no list.\n"
    f"At most {_MAX_TASKS} tasks. Reply with JSON only, no prose: "
    "{\"tasks\": [\"task one — check: …\", \"task two — check: …\"]} "
    "or {\"tasks\": []}.")

_REVISE_SYSTEM = (
    "You keep an AI coding agent's task list in line with what the user says "
    "while the agent works. You get the request the agent is working on, the "
    "task list (id, status, title) and the user's new message. Decide what "
    "the new message changes:\n"
    "- new work → a new task (id null) at the place it belongs; the user may "
    "say before, after or in between;\n"
    "- changed work → the same id with a new title;\n"
    "- work the user no longer wants → status \"skipped\";\n"
    "- work the user wants done again → status \"pending\";\n"
    "- work the user wants in another order → the same ids in the new order;\n"
    "- a question, a status ask, an \"ok\", or anything that changes no work "
    "→ the list exactly as it is.\n"
    "When the task list is empty, list only the new work the message adds "
    "(the request itself is already being worked on); none → {\"tasks\": []}.\n"
    "Copy every title the message does not change WORD FOR WORD, even one "
    "without a check; the rules below are for new and changed titles only.\n"
    + _RULES
    + "Never mark a task done — only the agent does that. Reply with JSON "
    "only, no prose, every existing task included, in the order the work "
    "should be done: {\"tasks\": [{\"id\": \"part-1\", \"title\": \"…\", "
    "\"status\": \"done\"}, {\"id\": null, \"title\": \"…\", \"status\": "
    "\"pending\"}]}.")


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_LLM_PLAN", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _timeout_s() -> int:
    try:
        return max(5, int(os.environ.get("AIFORGE_CHAT_LLM_PLAN_TIMEOUT_S", "30")))
    except ValueError:
        return 30


def _role(role: str) -> str:
    return os.environ.get("AIFORGE_CHAT_PLAN_ROLE", "").strip() or role or "chat"


def _stopped(session_id) -> bool:
    try:
        from aiforge_core.runtime import chat_cancel
        return bool(chat_cancel.is_cancelled(int(session_id)))
    except Exception:  # noqa: BLE001
        return False


def _ask(role: str, system: str, user: str, session_id,
         max_tokens: int = 1500) -> str | None:
    """The model's reply, or None when the call failed, ran out of time, or
    the user pressed Stop."""
    box: dict = {}

    def call():
        try:
            from aiforge_core.llm import client, model_wait
            with model_wait.optional():
                box["out"] = client.complete(_role(role), [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ], max_tokens=max_tokens, temperature=0, timeout_s=_timeout_s())
        except Exception as exc:  # noqa: BLE001 — a plan never blocks the turn
            _log.debug("task plan call failed: %s", exc)

    # Off the turn's thread so Stop and the time limit end the wait; the
    # turn's context rides along so the call is counted with the turn's.
    worker = threading.Thread(target=contextvars.copy_context().run, args=(call,),
                              daemon=True, name="task-plan")
    worker.start()
    until = time.monotonic() + _timeout_s() + 5
    while worker.is_alive() and time.monotonic() < until:
        if session_id is not None and _stopped(session_id):
            return None
        worker.join(0.2)
    out = box.get("out")
    return str(out) if out is not None else None


def _tasks_json(text: str | None):
    """The ``tasks`` list of the last JSON object in ``text`` that has one
    (a reasoning model thinks first and answers at the end), else None."""
    if not text:
        return None
    dec = json.JSONDecoder()
    found = None
    i = text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict) and isinstance(obj.get("tasks"), list):
            found = obj["tasks"]
        i = text.find("{", end)
    return found


def _clean(title) -> str:
    return " ".join(str(title or "").split())[:_MAX_TITLE]


def _earlier(messages, limit: int = 3000) -> str:
    """The conversation before the newest message, newest last, for resolving
    "both repos" and "the same": the user's words and the agent's answers,
    no tool output."""
    from .._context import _text_of
    from ._convo import strip_turn_note
    lines: list = []
    users = [i for i, m in enumerate(messages or [])
             if isinstance(m, dict) and m.get("role") == "user"]
    for m in (messages or [])[:users[-1] if users else 0]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        text = strip_turn_note(_text_of(m)).strip()
        if not text or text.startswith(("OBSERVATION", "[", "<<")):
            continue
        who = "USER" if m["role"] == "user" else "AGENT"
        lines.append(f"{who}: {' '.join(text.split())[:800]}")
    return "\n".join(lines)[-limit:]


def plan_asks(role: str, message: str, messages, cwd=None, session_id=None):
    """The refined task list for ``message`` (``[]``: no board), or None when
    the model could not give one (the caller falls back to the splitter)."""
    text = (message or "").strip()
    if len(text) < _MIN_CHARS:
        return []
    earlier = _earlier(messages)
    ask = ((f"EARLIER IN THIS CONVERSATION (context only, already handled):\n"
            f"{earlier}\n\n" if earlier else "")
           + (f"THE AGENT WORKS IN: {cwd}\n\n" if cwd else "")
           + f"THE USER'S NEWEST MESSAGE (plan this):\n{text[:8000]}")
    tasks = _tasks_json(_ask(role, _PLAN_SYSTEM, ask, session_id))
    if tasks is None:
        return None
    out = [t for t in (_clean(x.get("title") if isinstance(x, dict) else x)
                       for x in tasks if isinstance(x, (str, dict))) if t]
    if tasks and not out:
        return None                  # a shape we cannot read: not "no list"
    return out[:_MAX_TASKS] if len(out) >= 2 else []


_STATUSES = ("pending", "running", "done", "failed", "skipped")


def _next_slug(board: dict) -> str:
    n = len(board) + 1
    while f"msg-{n}" in board:
        n += 1
    return f"msg-{n}"


def _status_after(old: str, said: str) -> str:
    """What the reviser may do to an item's status: drop it, or put it back
    to do again. Done, running and failed are the agent's to set."""
    said = (said or "").strip().lower()
    if said == "skipped" and old in ("pending", "running"):
        return "skipped"
    if said == "pending" and old in ("done", "failed", "skipped"):
        return "pending"
    return old


def _only_check_added(old: str, new: str) -> bool:
    """``new`` is ``old`` with a "— check: …" tail and nothing else."""
    head = old.rstrip(" .")
    return new.startswith(head) and new[len(head):].lstrip(" .").lstrip("—–-").lstrip() \
        .lower().startswith("check:")


def _merged(board: dict, tasks: list) -> tuple[dict, list]:
    """The board after the reviser's list, and one line per change. An item
    the list leaves out is kept, after the item that came before it."""
    order: list = []
    new = {k: dict(v) for k, v in board.items()}
    changes: list = []
    room = max(0, 60 - len(board))
    for t in tasks:
        if not isinstance(t, dict):
            continue
        slug = str(t.get("id") or "").strip()
        title = _clean(t.get("title"))
        if slug in board and slug not in order:
            item = new[slug]
            # A title the reviser only added a "— check:" to is the same work.
            if title and title != _clean(item["title"]) \
                    and not _only_check_added(_clean(item["title"]), title):
                changes.append(f"changed {slug}: {title}")
                item["title"] = title
            status = _status_after(item["status"], str(t.get("status") or ""))
            if status == "pending" and status != item["status"]:
                # Work to do again is held at FINAL like any planned item
                # and is not closed for the agent when its answer is taken.
                item["from_request"] = False
                item["redo"] = True
            if status != item["status"]:
                changes.append(f"{'dropped' if status == 'skipped' else 'to do again'} "
                               f"{slug}: {item['title']}")
                item["status"] = status
            order.append(slug)
            continue
        if slug in board or not title or not room \
                or title in (v["title"] for v in new.values()):
            continue
        slug = _next_slug(new)          # new work (an unknown id counts as none)
        new[slug] = {"title": title, "status": "pending", "by_harness": True}
        changes.append(f"added {slug}: {title}")
        order.append(slug)
        room -= 1
    for i, slug in enumerate(board):
        if slug in order:
            continue
        before = [s for s in list(board)[:i] if s in order]
        order.insert(order.index(before[-1]) + 1 if before else 0, slug)
    if not changes and order != list(board):
        changes.append("new order: " + ", ".join(order))
    return {s: new[s] for s in order}, changes


def revise(role: str, board: dict, steers: list, goal: str, session_id=None):
    """Bring ``board`` in line with the mid-run message(s) ``steers``.
    Returns ``(board, changes)``: the new board and one line per change
    (``[]``: nothing changed), or ``(None, [])`` when the model could not
    say (the caller keeps the board and only tells the agent)."""
    said = "\n".join(f"- {' '.join(str(s).split())[:1500]}" for s in steers if str(s).strip())
    if not said:
        return board, []
    shown = [{"id": s, "status": it["status"], "title": it["title"]}
             for s, it in board.items()]
    ask = (f"THE REQUEST THE AGENT IS WORKING ON:\n{(goal or '')[:4000]}\n\n"
           f"THE TASK LIST NOW:\n{json.dumps(shown, ensure_ascii=False)}\n\n"
           f"THE USER'S NEW MESSAGE:\n{said}")
    tasks = _tasks_json(_ask(role, _REVISE_SYSTEM, ask, session_id,
                             max_tokens=1500 + 120 * len(board)))
    if tasks is None:
        return None, []
    new, changes = _merged(board, tasks)
    return (new, changes) if changes else (board, [])


__all__ = ["enabled", "plan_asks", "revise"]
