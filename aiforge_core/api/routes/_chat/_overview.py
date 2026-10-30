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
    waiting on the user) and ``decisions`` (newest last)."""
    from aiforge_core.runtime import chat_decisions, chat_goals, chat_store
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    g = chat_goals.goals(session_id)
    return {"goals": [{k: x.get(k) for k in _GOAL_KEYS if x.get(k) is not None}
                      for x in g.get("goals") or []],
            "now": list(g.get("now") or []),
            "current": g.get("current") or "",
            "asked": g.get("asked") or "",
            "decisions": chat_decisions.rows(session_id)[-limit:]}
