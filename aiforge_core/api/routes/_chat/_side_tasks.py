"""Side tasks: a second (third, …) agent run alongside the one a chat is on.

A chat session holds exactly one run — its Stop, steer, approvals and live
stream are all keyed by the session. So a message that arrives while a run is
in flight, and is an independent task rather than a correction, does not get
squeezed into that session. It becomes a CHILD chat: its own session, its own
run, the same folder. Everything per-session keeps working per task.

What this module decides:

* steer or task — a correction steers the running turn as before; a new
  request, a question, or "run another agent …" becomes a side task;
* now or queued — a task that only reads starts at once (within the model
  server's parallel slots). A task that edits files waits while another run in
  the same chat is editing, and starts by itself when that run ends;
* reporting back — when a side task finishes, its answer is appended to the
  parent chat, labelled, once the parent is not mid-turn.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

from fastapi import HTTPException
from pydantic import BaseModel, Field

from ._core import router

_log = logging.getLogger("aiforge.chat.side_tasks")
_LOCK = threading.RLock()

QUEUED, RUNNING, DONE, STOPPED = "queued", "running", "done", "stopped"

# The user asking, in so many words, for a second run.
_SIDE_CUE_RE = re.compile(
    r"\b(?:another|a\s+separate|a\s+second|a\s+new|one\s+more)\s+"
    r"(?:agent|task|chat|run)\b"
    r"|\bin\s+parallel\b|\bmeanwhile\b|\bin\s+the\s+meantime\b"
    r"|\bside\s+task\b|\bat\s+the\s+same\s+time\b|\bspin\s+(?:off|up)\b",
    re.IGNORECASE)
_ANSWER_CHARS = 12000
_CONTEXT_TURNS = 6
_CONTEXT_CHARS = 400


class _SideBody(BaseModel):
    content: str = Field(..., min_length=1)
    mode: str = Field("simple", description="simple | plan | team — for a task")
    as_: str = Field("auto", alias="as",
                     description="auto | task | steer — auto lets the server decide")

    model_config = {"populate_by_name": True}


# ── deciding ─────────────────────────────────────────────────────────────────

_INFO_OPENER_RE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:answer|explain|describe|summari[sz]e|list|show|tell|give\s+me|what|"
    r"which|who|whom|whose|why|how|where|when|is\s+there|are\s+there|"
    r"do\s+(?:we|you|i)|does|did|is|are|was|were)\b", re.IGNORECASE)
_POLITE_RE = re.compile(r"^\s*(?:please\s+)?(?:can|could|would|will)\s+you\b",
                        re.IGNORECASE)
_POLITE_INFO_RE = re.compile(
    r"^\s*(?:please\s+)?(?:can|could|would|will)\s+you\s+(?:please\s+)?"
    r"(?:answer|explain|describe|summari[sz]e|list|show|tell|give\s+me)\b",
    re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"(?:^|\s)(?:\d+[.)]|[-*•])\s+\S")


def asks_for_information(text: str) -> bool:
    """Whether a mid-run message only wants something ANSWERED.

    A request for information does not change the work in progress, so it must
    not be folded into it: the running agent took "answer these questions" as
    its new task, replied to them as its final answer and never finished what
    it was doing. A message that touches the work (it names a change to make)
    stays a steer."""
    t = (text or "").strip()
    if not t:
        return False
    from ._overlap import has_edit_intent
    if has_edit_intent(t):
        return False
    # "can you also log the date?" is an instruction; "can you explain X?" is not.
    if _POLITE_RE.match(t) and not _POLITE_INFO_RE.match(t):
        return False
    if "?" in t or _INFO_OPENER_RE.match(t):
        return True
    if len(_LIST_ITEM_RE.findall(t)) >= 2:       # a numbered list of asks
        return True
    if len(t) > 200 or t.count("\n") >= 3:       # a paragraph is a request of its own
        return True
    return False


# An instruction about the work: "use X", "switch to Y", "go with Z", "do it",
# "implement …", "instead", "rather than", or a stated fact that changes the
# plan ("we have rewritten it in …", "we can't run …"). It touches the running
# work, so a '?' (even "…right ?") does not make it a separate question.
_DIRECTIVE_RE = re.compile(
    r"^\W*(?:(?:no|nope|ok(?:ay)?|actually|also|and|then|so|but|please|pls)\b[\s,.:;-]*)*"
    r"(?:use|switch|go\s+with|do\s+it|implement|build|write|port|rewrite|"
    r"stick\s+(?:to|with)|continue\s+with|proceed\s+with|focus\s+on)\b"
    r"|\b(?:instead|rather\s+than|switch\s+to|go\s+with|implement\s+(?:it|this|that)"
    r"|do\s+not\s+(?:review|run)|don'?t\s+(?:review|run))\b"
    r"|\bwe\s+(?:have|had|'ve|already|just|can'?t|cannot|no\s+longer|"
    r"rewrote|rewritten|replaced|moved|switched|migrated|use|are\s+using)\b",
    re.IGNORECASE)


def is_directive(text: str) -> bool:
    """An instruction or correction aimed at the work in progress, not a
    question that merely ends in '?'. A message that opens as a plain question
    ("do we have …", "what does …") is not one."""
    t = (text or "").strip()
    return bool(t and _DIRECTIVE_RE.search(t) and not _INFO_OPENER_RE.match(t))


def running_mode(session_id: int) -> str:
    """The mode (simple/plan/team) of the turn now running in this chat: the
    mode its latest user message was sent with."""
    try:
        from aiforge_core.runtime import chat_store
        last = _last(chat_store.get_messages(session_id) or [], "user") or {}
        return last.get("mode") or "simple"
    except Exception:  # noqa: BLE001
        return "simple"


def classify(text: str, mode: str = "simple") -> str:
    """``"steer"`` or ``"task"`` for a message typed while a run is going.

    Stopping or replacing the run, and anything that reads as an adjustment to
    it, steers. An explicit ask for another agent, a question, or a new
    request is its own task. While a TEAM/pipeline run is going the default is
    to steer: only an explicit side cue or a plain question is a task."""
    t = (text or "").strip()
    if not t:
        return "steer"
    try:
        from aiforge_core.runtime.run_interrupt import (
            text_cuts_running_work,
            text_replaces_work,
        )
        if text_cuts_running_work(t) or text_replaces_work(t):
            return "steer"
    except Exception:  # noqa: BLE001
        pass
    if _SIDE_CUE_RE.search(t):
        return "task"
    if is_directive(t):
        return "steer"
    if mode == "team":
        return "task" if (_INFO_OPENER_RE.match(t)
                          and asks_for_information(t)) else "steer"
    if asks_for_information(t):
        return "task"
    from ._sched_fold import _is_new_request
    return "task" if _is_new_request(t) else "steer"


def edits_files(text: str, mode: str) -> bool:
    """Whether a task is expected to change files. Team runs always are; a plan
    never is; otherwise the wording decides."""
    if mode == "team":
        return True
    if mode == "plan":
        return False
    from ._overlap import has_edit_intent
    return has_edit_intent(text)


def parallel_limit(role: str = "chat") -> int:
    """Runs one chat family may have going at once (the parent included):
    ``AIFORGE_CHAT_SIDE_TASKS_MAX``, else what the model server serves in
    parallel. 1 means a side task waits for the running turn."""
    raw = os.environ.get("AIFORGE_CHAT_SIDE_TASKS_MAX", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    try:
        from aiforge_core.llm import slots
        return max(1, int(slots.llm_slots(role)))
    except Exception:  # noqa: BLE001
        return 1


# ── state helpers ────────────────────────────────────────────────────────────

def _is_running(session_id: int) -> bool:
    from aiforge_core.runtime import chat_runs
    return chat_runs.is_running(session_id)


def _last(rows: list, role: str) -> "dict | None":
    return next((m for m in reversed(rows) if m.get("role") == role), None)


def _parent_edits(parent: dict) -> bool:
    """Whether the parent's running turn is one that changes files."""
    from aiforge_core.runtime import chat_store
    last_user = _last(chat_store.get_messages(parent["id"]) or [], "user") or {}
    return edits_files(last_user.get("content") or "",
                       last_user.get("mode") or "simple")


def _set_task(child: dict, **fields) -> dict:
    from aiforge_core.runtime import chat_store
    task = {**(child.get("task") or {}), **fields}
    chat_store.set_session_task(child["id"], child.get("parent_id"), task)
    child["task"] = task
    return child


def _settle_finished(child: dict) -> dict:
    """A task marked running whose run is gone has ended: done when it left an
    answer, stopped when it did not (Stop, a crash, or an API restart)."""
    task = child.get("task") or {}
    if task.get("state") != RUNNING or _is_running(child["id"]):
        return child
    from aiforge_core.runtime import chat_store
    rows = chat_store.get_messages(child["id"]) or []
    answered = bool(rows) and rows[-1].get("role") == "assistant" \
        and bool((rows[-1].get("content") or "").strip())
    return _set_task(child, state=DONE if answered else STOPPED,
                     ended=time.time())


def _children(parent_id: int) -> list[dict]:
    from aiforge_core.runtime import chat_store
    return [_settle_finished(c) for c in chat_store.child_sessions(parent_id)]


# ── starting ─────────────────────────────────────────────────────────────────

def _context_preamble(parent: dict) -> str:
    """A short view of the parent conversation, so the side agent knows what
    "that file" or "the bug" refers to."""
    from aiforge_core.runtime import chat_store
    rows = [m for m in chat_store.get_messages(parent["id"]) or []
            if m.get("role") in ("user", "assistant")
            and (m.get("content") or "").strip()][-_CONTEXT_TURNS:]
    if not rows:
        return ""
    lines = [f"{m['role']}: {' '.join((m.get('content') or '').split())[:_CONTEXT_CHARS]}"
             for m in rows]
    return ("This is a SIDE TASK started from another chat that is still "
            "working on its own request. Do only what is asked below; do not "
            "continue that chat's work. Its recent messages, for reference "
            "only:\n" + "\n".join(lines) + _live_status(parent)
            + "\n\nThe side task:\n")


def _live_status(parent: dict) -> str:
    """What that chat's run is doing right now, so a side agent asked about it
    ("which file is it on?", "is it stuck?") answers from fact."""
    try:
        from aiforge_core.runtime import chat_runs, chat_status
        run = chat_runs.get(parent["id"])
        if run is None or run.done:
            return ""
        from aiforge_core.runtime import cmd_jobs
        return ("\n\nLive status of that chat's running work (read-only; you "
                "are not to touch it):\n"
                + chat_status.render(chat_status.snapshot(
                    run, jobs=cmd_jobs.for_session(parent["id"]))))
    except Exception:  # noqa: BLE001 — a status line never blocks a task
        return ""


def _start(child: dict, parent: dict) -> None:
    """Start the child's run on its own producer thread."""
    from ._message import _SessionMsgBody, chat_session_message
    task = child.get("task") or {}
    _set_task(child, state=RUNNING, started=time.time())
    try:
        # The response is a lazy stream nobody reads here: the run lives on its
        # own thread and the UI attaches to it like any other chat.
        chat_session_message(child["id"], _SessionMsgBody(
            content=_context_preamble(parent) + (task.get("prompt") or ""),
            mode=task.get("mode") or "simple"))
    except Exception as exc:  # noqa: BLE001
        _log.warning("side task %s failed to start: %s", child["id"], exc)
        _set_task(child, state=STOPPED, ended=time.time(), error=str(exc)[:300])


def pump(parent_id: int) -> None:
    """Start every queued task that may run now."""
    from aiforge_core.runtime import chat_store
    with _LOCK:
        parent = chat_store.get_session(parent_id)
        if not parent:
            return
        kids = _children(parent_id)
        parent_running = _is_running(parent_id)
        running = [c for c in kids if (c.get("task") or {}).get("state") == RUNNING]
        count = len(running) + (1 if parent_running else 0)
        editing = (parent_running and _parent_edits(parent)) or any(
            (c.get("task") or {}).get("edits") for c in running)
        limit = parallel_limit(parent.get("role") or "chat")
        for c in kids:
            task = c.get("task") or {}
            if task.get("state") != QUEUED:
                continue
            if count >= limit or (task.get("edits") and editing):
                continue
            _start(c, parent)
            count += 1
            editing = editing or bool(task.get("edits"))


def create(parent_id: int, content: str, mode: str = "simple") -> dict:
    """Make a side task under ``parent_id`` and start it if it may run now."""
    from aiforge_core.runtime import chat_store
    parent = chat_store.get_session(parent_id)
    if not parent:
        raise HTTPException(404, f"session {parent_id} not found")
    if parent.get("parent_id"):
        # A side task's own side tasks belong to the same family.
        parent = chat_store.get_session(parent["parent_id"]) or parent
    mode = mode if mode in ("simple", "plan", "team") else "simple"
    text = content.strip()
    title = " ".join(text.split())[:60] or "Side task"
    with _LOCK:
        child = chat_store.create_session(title, parent.get("cwd"),
                                          role=parent.get("role") or "chat")
        chat_store.set_session_task(child["id"], parent["id"], {
            "state": QUEUED, "prompt": text, "mode": mode,
            "edits": edits_files(text, mode), "posted": False,
            "created": time.time()})
        # Same workspace as the chat it belongs to: it reads what that chat has
        # written and edits are serialised (see pump), not copied.
        if parent.get("workdir"):
            chat_store.set_session_workdir(child["id"], parent["workdir"])
    pump(parent["id"])
    return _view(chat_store.get_session(child["id"]) or child)


# ── reporting back ───────────────────────────────────────────────────────────

def _post_results(parent_id: int) -> int:
    """Append each finished, unposted side task's answer to the parent chat.
    Waits (returns 0) while the parent is mid-turn so its own turn's rows stay
    together."""
    from aiforge_core.runtime import chat_store
    with _LOCK:
        if _is_running(parent_id) or not chat_store.get_session(parent_id):
            return 0
        posted = 0
        for c in _children(parent_id):
            task = c.get("task") or {}
            if task.get("posted") or task.get("state") not in (DONE, STOPPED):
                continue
            if task.get("state") == DONE:
                answer = _last(chat_store.get_messages(c["id"]) or [],
                               "assistant") or {}
                prompt = " ".join((task.get("prompt") or "").split())[:200]
                chat_store.add_message(
                    parent_id, "assistant",
                    f"**Side task:** {prompt}\n\n{(answer.get('content') or '').strip()}",
                    [{"type": "side_task", "session_id": c["id"],
                      "title": c.get("title")}],
                    mode=task.get("mode") or "simple")
                posted += 1
            _set_task(c, posted=True)
        return posted


def on_run_finished(session_id: int) -> None:
    """A chat run ended: settle it if it was a side task, post finished side
    tasks back, and start whatever was waiting on it."""
    from aiforge_core.runtime import chat_store
    sess = chat_store.get_session(session_id)
    if not sess:
        return
    parent_id = sess.get("parent_id") or session_id
    if not sess.get("parent_id") and not chat_store.child_sessions(session_id):
        return                           # an ordinary chat with no side tasks
    _post_results(parent_id)
    pump(parent_id)


def install() -> None:
    from aiforge_core.runtime import chat_runs
    chat_runs.on_finish(on_run_finished)


install()


# ── API ──────────────────────────────────────────────────────────────────────

_QUIET_STATUS_S = 20.0


def _live_state(child: dict) -> str:
    """What a running side task is doing, so the chat never shows a bare
    spinner. Silence is named: with a single model slot the side run is
    usually waiting for the model behind the main run."""
    try:
        from aiforge_core.runtime import chat_runs
        run = chat_runs.get(child["id"])
        if run is None or run.done:
            return "starting…"
        quiet = time.time() - run.last_event_at
        phase = run.phase or ""
        if phase and quiet < _QUIET_STATUS_S:
            return phase
        parent_id = child.get("parent_id")
        ahead = 0
        if parent_id and _is_running(parent_id):
            ahead = 1
        if parent_id:
            ahead += sum(1 for c in _children(parent_id)
                         if c["id"] != child["id"]
                         and (c.get("task") or {}).get("state") == RUNNING)
        mins = f"{int(quiet)}s" if quiet < 90 else f"{int(quiet // 60)}m"
        if ahead:
            return (f"waiting for the model, queued behind {ahead} other "
                    f"run{'s' if ahead != 1 else ''} (quiet {mins})")
        return f"{phase or 'waiting for the model'} (quiet {mins})"
    except Exception:  # noqa: BLE001
        return ""


def _view(child: dict) -> dict:
    from aiforge_core.runtime import chat_store
    task = child.get("task") or {}
    state = task.get("state") or QUEUED
    preview, full = "", ""
    if state == DONE:
        answer = _last(chat_store.get_messages(child["id"]) or [], "assistant") or {}
        full = (answer.get("content") or "").strip()[:_ANSWER_CHARS]
        preview = " ".join(full.split())[:240]
    return {"id": child["id"], "title": child.get("title"), "state": state,
            "prompt": task.get("prompt") or "", "mode": task.get("mode") or "simple",
            "edits": bool(task.get("edits")), "posted": bool(task.get("posted")),
            "created": task.get("created"), "started": task.get("started"),
            "ended": task.get("ended"), "error": task.get("error"),
            "preview": preview,
            "status": _live_state(child) if state == RUNNING else "",
            # The whole answer, so the chat can show it the moment it is ready
            # instead of after the main run ends.
            "answer": full}


def status_of(session_id: int, run) -> dict:
    """The answer to "what is the status?" for a live run — from the run's own
    record, no model call."""
    from aiforge_core.runtime import chat_interject, chat_status
    try:
        pending = len(chat_interject.peek_texts(session_id))
    except Exception:  # noqa: BLE001
        pending = 0
    try:
        tasks = [_view(c) for c in _children(session_id)]
    except Exception:  # noqa: BLE001
        tasks = []
    try:
        from aiforge_core.runtime import cmd_jobs
        jobs = cmd_jobs.for_session(session_id)
    except Exception:  # noqa: BLE001
        jobs = []
    try:
        from aiforge_core.runtime import handoff_store
        ho = handoff_store.view(session_id).get("handoff")
    except Exception:  # noqa: BLE001
        ho = None
    snap = chat_status.snapshot(run, pending_steers=pending, side_tasks=tasks,
                                jobs=jobs, handoff=ho)
    return {"text": chat_status.render(snap), "snapshot": snap}


@router.get("/api/chat/sessions/{session_id}/status",
            responses={404: {"description": "Not found"}})
def chat_run_status(session_id: int) -> dict:
    """What the session's run is doing right now (no model involved)."""
    from aiforge_core.runtime import chat_runs, chat_store
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    run = chat_runs.get(session_id)
    if run is None or run.done:
        text = "**Status** — nothing is running in this chat."
        try:
            from aiforge_core.runtime import chat_status, handoff_store
            v = handoff_store.view(session_id)
            if v["unfinished"]:
                text += ("\nThe last turn ended unfinished"
                         f" ({v['handoff'].get('status')}). Say \"continue\" "
                         "and it picks up from this:\n"
                         + "\n".join(chat_status.handoff_lines(v["handoff"])))
        except Exception:  # noqa: BLE001
            pass
        return {"running": False, "text": text}
    return {"running": True, **status_of(session_id, run)}


@router.get("/api/chat/sessions/{session_id}/tasks",
            responses={404: {"description": "Not found"}})
def chat_side_tasks(session_id: int) -> dict:
    """The side tasks of a chat, oldest first, with their state."""
    from aiforge_core.runtime import chat_store
    sess = chat_store.get_session(session_id)
    if not sess:
        raise HTTPException(404, f"session {session_id} not found")
    # Also the catch-up after an API restart: nothing else would notice that a
    # task finished, or start one that was queued.
    _post_results(session_id)
    pump(session_id)
    return {"tasks": [_view(c) for c in _children(session_id)],
            "limit": parallel_limit(sess.get("role") or "chat"),
            "running": _is_running(session_id)}


@router.post("/api/chat/sessions/{session_id}/side",
             responses={404: {"description": "Not found"}})
def chat_side_message(session_id: int, body: _SideBody) -> dict:
    """A message typed while this chat is busy. Returns what was done with it:
    ``{"action": "steer", …}`` folded into the running turn,
    ``{"action": "task", "task": …}`` started (or queued) as a side task, or
    ``{"action": "send"}`` when nothing is running and it is an ordinary turn."""
    from aiforge_core.runtime import chat_store
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    want = body.as_ if body.as_ in ("task", "steer") else "auto"
    if want != "task" and not _is_running(session_id):
        return {"action": "send"}
    if want != "task":
        from aiforge_core.runtime import chat_runs, chat_status
        if chat_status.is_status_request(body.content):
            run = chat_runs.get(session_id)
            if run is not None and not run.done:
                return {"action": "status", **status_of(session_id, run)}
    if want == "auto":
        want = classify(body.content, running_mode(session_id))
    if want == "steer":
        from aiforge_core.runtime import chat_runs, chat_status

        from ._message import _SteerBody, chat_session_steer
        res = chat_session_steer(session_id, _SteerBody(content=body.content))
        if res.get("queued"):
            run = chat_runs.get(session_id)
            res["where"] = chat_status.waiting_on(run) if run is not None else ""
        if res.get("queued") or not res.get("unsupported"):
            return {"action": "steer", **res}
        # This run cannot be steered (best-of-N): the message still deserves
        # an answer, so it runs beside it.
    return {"action": "task", "task": create(session_id, body.content, body.mode)}
