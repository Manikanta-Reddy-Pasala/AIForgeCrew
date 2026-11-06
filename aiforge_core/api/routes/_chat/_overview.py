"""The Run overview panel: what a chat is working toward and what changed.

The goals of the chat (runtime/chat_goals) and the lines that changed the
course of the work (runtime/chat_decisions). The task list itself comes from
the turn's own events, which the page already has. The web UI polls this
while a run goes on and once when a turn ends.
"""
from __future__ import annotations

from fastapi import HTTPException, Query

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
    return {"items": items, "active": any(i["state"] == "running" for i in items)}


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
            "active": any(i["state"] == "running" for i in items)}
