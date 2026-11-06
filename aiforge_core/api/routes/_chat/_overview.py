"""The Run overview panel: what a chat is working toward and what changed.

The goals of the chat (runtime/chat_goals) and the lines that changed the
course of the work (runtime/chat_decisions). The task list itself comes from
the turn's own events, which the page already has. The web UI polls this
while a run goes on and once when a turn ends.
"""
from __future__ import annotations

from fastapi import HTTPException, Query
from pydantic import BaseModel

from ._core import router

_GOAL_KEYS = ("id", "text", "status", "turn", "closed_turn")


@router.get("/api/chat/sessions/{session_id}/overview",
            responses={404: {"description": "Not found"}})
def chat_overview(session_id: int,
                  limit: int = Query(150, ge=1, le=150)) -> dict:
    """``goals`` (open, done, dropped), ``now`` (ids worked on), ``current``
    (the whole of the work now, in one statement), ``asked`` (a question
    waiting on the user), ``decisions`` (newest last) and ``turn`` (the
    chat's turn now: rows of older turns carry a smaller one)."""
    from aiforge_core.runtime import chat_decisions, chat_goals, chat_store
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    g = chat_goals.goals(session_id)
    return {"goals": [{k: x.get(k) for k in _GOAL_KEYS if x.get(k) is not None}
                      for x in g.get("goals") or []],
            "now": list(g.get("now") or []),
            "current": g.get("current") or "",
            "asked": g.get("asked") or "",
            "decisions": chat_decisions.rows(session_id)[-limit:],
            "turn": chat_decisions.current_turn(session_id)}


@router.get("/api/chat/sessions/{session_id}/background",
            responses={404: {"description": "Not found"}})
def chat_background_work(session_id: int) -> dict:
    """The chat's work outside a turn — watches, background commands and
    scheduled jobs — for the progress panel: ``items`` (``kind``, ``title``,
    ``state`` running|scheduled, times) and ``active`` (something busy now)."""
    from aiforge_core.runtime import chat_background, chat_store
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    items = chat_background.items(session_id)
    return {"items": items, "active": any(i["state"] == "running" for i in items),
            "stoppable": chat_background.stoppable(items)}


@router.post("/api/chat/sessions/{session_id}/background/stop",
             responses={404: {"description": "Not found"}})
def chat_background_stop(session_id: int) -> dict:
    """Stop the chat's watches, background commands and the scheduled run in
    flight (the schedule stays; a live turn and the chat's services do not
    stop). Returns what stopped and the background left."""
    from aiforge_core.runtime import chat_background, chat_store
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    stopped = chat_background.stop(session_id)
    items = chat_background.items(session_id)
    return {"stopped": stopped, "items": items,
            "active": any(i["state"] == "running" for i in items),
            "stoppable": chat_background.stoppable(items)}


class _GoalEdit(BaseModel):
    text: "str | None" = None
    status: "str | None" = None


class _GoalNew(BaseModel):
    text: str


def _note(out: dict) -> str:
    from aiforge_core.runtime import chat_decisions
    what = "; ".join(chat_decisions.goal_lines(out.get("changes") or [out["change"]]))
    head = ("[GOALS — the user edited the goals in the progress panel, not a new "
            "message] " + what + ". ")
    if out.get("current"):
        return head + (f"GOAL NOW (all of it): {out['current']}. Work only toward it from "
                       "this step: stop at once anything that serves a dropped goal (do "
                       "not finish it), and start a new or changed goal now.")
    if not out.get("emptied"):
        # no goal list in hand (a typed request runs): the edit touched none
        return head + ("Keep on with the work in hand; follow this change only where "
                       "that work touches the edited goal.")
    rest = out.get("open") or []
    return head + ("No goal is in hand now: stop the work on what was dropped or finished, "
                   "and reply in one or two lines with where it stands."
                   + (" Still open, not for this turn: " + "; ".join(rest) if rest else ""))


def _running(session_id: int) -> bool:
    from aiforge_core.runtime import chat_interject
    return chat_interject.reads_goals(session_id)


def _after_goal_edit(session_id: int, out: dict) -> dict:
    """The user's edit is their decision; a running agent hears it at its next
    step (as the goals now, not as a new message to interpret)."""
    from aiforge_core.runtime import chat_decisions, chat_interject
    chat_decisions.record(session_id, chat_decisions.goal_lines(out.get("changes") or [out["change"]]),
                          by="you", kind="goal", why="edited in the progress panel")
    told = chat_interject.push_goals(session_id, _note(out))
    return {**chat_overview(session_id, limit=150), "changed": out["change"], "told_agent": told}


@router.patch("/api/chat/sessions/{session_id}/goals/{goal_id}",
              responses={404: {"description": "Not found"}, 400: {"description": "Bad status"}})
def chat_goal_edit(session_id: int, goal_id: str, body: _GoalEdit) -> dict:
    """Edit a goal's words or status (open / done / dropped) from the panel."""
    from aiforge_core.runtime import chat_goals, chat_store
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    try:
        out = chat_goals.user_edit(session_id, goal_id, text=body.text, status=body.status,
                                   in_hand=_running(session_id))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if out is None:
        raise HTTPException(404, f"goal {goal_id} not found or unchanged")
    return _after_goal_edit(session_id, out)


@router.delete("/api/chat/sessions/{session_id}/goals/{goal_id}",
               responses={404: {"description": "Not found"}})
def chat_goal_delete(session_id: int, goal_id: str) -> dict:
    """Drop a goal from the panel: the agent does no more work on it."""
    from aiforge_core.runtime import chat_goals, chat_store
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    out = chat_goals.user_edit(session_id, goal_id, status="dropped")
    if out is None:
        raise HTTPException(404, f"goal {goal_id} not found or already dropped")
    return _after_goal_edit(session_id, out)


@router.post("/api/chat/sessions/{session_id}/goals",
             responses={404: {"description": "Not found"}})
def chat_goal_add(session_id: int, body: _GoalNew) -> dict:
    """Add a goal from the panel."""
    from aiforge_core.runtime import chat_goals, chat_store
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    out = chat_goals.user_add(session_id, body.text, in_hand=_running(session_id))
    if out is None:
        raise HTTPException(400, "empty goal")
    return _after_goal_edit(session_id, out)
