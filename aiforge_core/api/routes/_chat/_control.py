"""Session control endpoints: checkpoints, promote to ticket, and next-step
suggestion feedback."""
from __future__ import annotations

import os

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from aiforge_core.tickets import store as tickets_mod

from ._core import (
    _NEW_CHAT,
    _default_cwd,
    router,
)


class _CheckpointBody(BaseModel):
    label: str | None = Field(None, description="human label for the snapshot")


@router.get("/api/chat/sessions/{session_id}/checkpoints", responses={404: {"description": "Not found"}})
def chat_session_checkpoints(session_id: int) -> dict:
    """List workspace checkpoints (#3) for this session's working dir."""
    from aiforge_core.runtime import chat_store, checkpoints
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = session.get("cwd") or _default_cwd()
    return {"checkpoints": checkpoints.list_checkpoints(cwd)}


@router.post("/api/chat/sessions/{session_id}/checkpoints", status_code=201, responses={404: {"description": "Not found"}})
def chat_session_checkpoint_create(session_id: int, body: _CheckpointBody) -> dict:
    """Snapshot the session's working dir (#3) to a hidden git ref."""
    import datetime as _dt

    from aiforge_core.runtime import chat_store, checkpoints
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = session.get("cwd") or _default_cwd()
    when = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return checkpoints.snapshot(cwd, label=body.label or "manual", when=when)


class _RestoreBody(BaseModel):
    sha: str = Field(..., min_length=4)
    paths: list[str] | None = Field(
        None, description="restore ONLY these paths (files-only / subset restore); "
                          "omit to restore the whole snapshot")
    delete_orphans: bool = Field(
        False, description="full-state restore: also delete files created after "
                           "the checkpoint so the tree exactly matches it")


@router.post("/api/chat/sessions/{session_id}/checkpoints/restore", responses={404: {"description": "Not found"}})
def chat_session_checkpoint_restore(session_id: int, body: _RestoreBody) -> dict:
    """Restore the session's working dir to a checkpoint (#3).

    Granularity: ``paths`` restores a subset; ``delete_orphans`` makes it a
    full-state restore (matching the snapshot exactly)."""
    from aiforge_core.runtime import chat_store, checkpoints
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = session.get("cwd") or _default_cwd()
    return checkpoints.restore(cwd, body.sha, paths=body.paths or None,
                               delete_orphans=bool(body.delete_orphans))


class _SessionTicketBody(BaseModel):
    content: str = Field(..., min_length=1)
    project: str | None = Field(None, description="target repo; defaults to session cwd name")


@router.post("/api/chat/sessions/{session_id}/ticket", status_code=201, responses={404: {"description": "Not found"}})
def chat_session_ticket(session_id: int, body: _SessionTicketBody) -> dict:
    """Pipeline mode: turn a chat message into a real ticket that runs the
    full architect→planner→verifier→doer→feedback→learner pipeline. The
    runner picks it up (urgent priority → next); the chat UI streams live
    stage updates from ``/api/trace/{identifier}/stream``. Returns the
    created ticket identifier + trace stream path."""
    from aiforge_core.runtime import chat_store
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    project = (body.project or "").strip() or os.path.basename(
        os.path.normpath(session.get("cwd") or _default_cwd())) or None
    title = body.content.strip().splitlines()[0][:120] or "chat request"
    if (session.get("title") or _NEW_CHAT) == _NEW_CHAT:
        chat_store.rename_session(session_id, title)
    t = tickets_mod.create(
        title=title, body=body.content.strip(), project=project,
        priority="urgent", route="code",
        # interactive=chat → the runner's clarify step may ask questions
        # before running. Normal tickets omit this → static, no ask.
        metadata={"source": "chat", "chat_session_id": session_id,
                  "interactive": True},
    )
    chat_store.add_message(session_id, "user", body.content)
    chat_store.add_message(
        session_id, "assistant",
        f"Started pipeline run as **{t.identifier}** (project `{project or '—'}`). "
        f"Streaming stage updates…",
        [{"type": "ticket", "identifier": t.identifier, "project": project}],
    )
    return {"ticket": t.identifier, "ticket_id": t.id, "project": project,
            "trace_url": f"/api/tickets/{t.identifier}/events/stream"}


@router.post("/api/chat/suggestion/{prediction_id}")
async def suggestion_outcome(prediction_id: str, request: Request) -> dict:
    """Record what the user did with a predicted next step.

    BOTH answers are recorded. A feature that learns only from its successes
    drifts, and a dismissal is the clearer signal of the two — it says the
    prediction was wrong about this user, which is exactly what the next one
    needs to know.

    An unknown id is a no-op rather than a 404: a chip in a browser tab left
    open across a restart is not an error the user can do anything about.

    Accepting an OFFER deliberately does NOT execute anything here. The chip
    sends the action back as an ordinary chat message, so it passes through the
    same approval gates, the same tool policy and the same transcript as
    anything else the user asks for. A second execution path that bypassed
    those gates is the hole this feature must not open.
    """
    from aiforge_core.runtime import next_step

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — a bodyless click is a dismissal
        payload = {}
    accepted = bool((payload or {}).get("accepted"))
    next_step.outcome(prediction_id, accepted,
                      edited=str((payload or {}).get("edited") or ""))
    return {"ok": True, "accepted": accepted}


@router.get("/api/chat/suggestions")
def suggestion_history(limit: int = 20) -> dict:
    """What has been predicted and what the user did with it.

    The counters that answer "is this feature good enough to extend to the
    pipeline" — which is the decision the design deliberately left open.
    """
    from aiforge_core.runtime import next_step

    rows = next_step.history(max(1, min(int(limit or 20), 200)))
    return {"suggestions": rows,
            "accepted": sum(1 for r in rows if r.get("accepted") is True),
            "dismissed": sum(1 for r in rows if r.get("accepted") is False)}
