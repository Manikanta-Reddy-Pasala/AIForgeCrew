"""Ticket control endpoints: comments, route preview and override, intervening
in a running task, answering its question, and the live event stream."""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from aiforge_core.api.routes._sse import sse_response
from aiforge_core.tickets import store as tickets_mod

from .tickets_rows import _ticket_row_out

router = APIRouter()


class RouteUpdate(BaseModel):
    route: str                                  # 'code' | 'workflow'
    route_workflow: str | None = None           # required when route='workflow'
    route_source: str = "manual"                # default to manual for UI overrides
    route_confidence: float | None = None


class RoutePreview(BaseModel):
    title: str = ""
    body: str
    attachments: list[str] = Field(default_factory=list)
    intent: dict | None = None


class CommentCreate(BaseModel):
    body: str
    author: str = "human"


@router.post("/api/workflows/preview")
def workflow_preview(payload: RoutePreview) -> dict:
    """Run the route detector against a candidate ticket WITHOUT
    creating it. UI debounces this on body change to show the
    detected workflow chip live."""
    from aiforge_core.workflows.detector import preview
    return preview(
        body=payload.body, title=payload.title,
        attachments=payload.attachments, intent=payload.intent,
    )


@router.put("/api/tickets/{identifier}/route", responses={400: {"description": "Bad request"}, 404: {"description": "Not found"}})
def override_route(identifier: str, payload: RouteUpdate) -> dict:
    """Manual route override — UI 'override' link calls this. Sets
    route_source='manual' by default so the audit trail distinguishes
    operator overrides from auto-detected picks."""
    if payload.route == "workflow":
        from aiforge_core.workflows import get as _get_wf
        if not payload.route_workflow:
            raise HTTPException(400, "route='workflow' requires route_workflow")
        if _get_wf(payload.route_workflow) is None:
            raise HTTPException(
                400, f"unknown workflow id: {payload.route_workflow!r}",
            )
    t = tickets_mod.update_route(
        identifier,
        route=payload.route,
        route_workflow=payload.route_workflow,
        route_source=payload.route_source,
        route_confidence=payload.route_confidence,
    )
    if t is None:
        raise HTTPException(404, f"ticket {identifier} not found")
    return _ticket_row_out({
        "id": t.id, "identifier": t.identifier, "title": t.title,
        "body": t.body, "status": t.status, "priority": t.priority,
        "assignee_role": t.assignee_role, "parent_id": t.parent_id,
        "branch": t.branch, "project": t.project, "labels": t.labels,
        "metadata": t.metadata, "created_at": t.created_at,
        "updated_at": t.updated_at, "completed_at": t.completed_at,
        "route": t.route, "route_workflow": t.route_workflow,
        "route_source": t.route_source, "route_confidence": t.route_confidence,
    })


@router.post("/api/tickets/{identifier}/comments", status_code=201, responses={404: {"description": "Not found"}})
def add_comment(identifier: str, payload: CommentCreate) -> dict:
    t = tickets_mod.get(identifier)
    if t is None:
        raise HTTPException(404, f"ticket {identifier} not found")
    eid = tickets_mod.add_comment(t.id, payload.author, payload.body)
    return {"event_id": eid}


# ─────────── Live agent intervention (GA _stop / _keyinfo / _intervene) ────
# Uses GA's task-intervention mechanism (commit 62ac73c). Harness writes
# control files into the running agent's task_dir; GA's turn_end_callback
# polls them and applies. Lets us steer or stop a live agent without
# restarting the runtime.


def _resolve_active_task_dirs(identifier: str) -> list[str]:
    """Return GA temp dirs that match a running agent for this ticket."""
    # AIFORGE_GA_DIR override first, else the genericagent checkout in the
    # running user's home — no hardcoded per-operator absolute paths.
    ga_root_candidates = (
        os.environ.get("AIFORGE_GA_DIR", ""),
        os.path.expanduser("~/genericagent"),
    )
    # The identifier comes off an HTTP request. One segment or nothing: a
    # ticket id containing "../" is not a ticket id, and sanitising it quietly
    # would hide that.
    from aiforge_core.config.safe_paths import safe_dir, safe_segment
    ident = safe_segment(identifier)
    if not ident:
        return []
    prefixes = (f"aiforge-{ident}-", f"aiforge-planner-{ident}-")
    for root in ga_root_candidates:
        base = safe_dir(os.path.join(root, "temp")) if root else ""
        if not base:
            continue
        # List the directory and keep the entries whose NAME starts with the
        # identifier, rather than building `glob(f"...{ident}-*")`: the
        # identifier is compared, never joined into a path. Same answer, and
        # nothing off the request reaches the filesystem call.
        try:
            with os.scandir(base) as entries:
                return sorted(os.path.join(base, e.name) for e in entries
                              if e.is_dir() and e.name.startswith(prefixes))
        except OSError:
            return []
    return []


@router.post("/api/tickets/{identifier}/intervene", responses={400: {"description": "Bad request"}, 404: {"description": "Not found"}})
def intervene(identifier: str, payload: dict) -> dict:
    """Inject a runtime instruction into a running agent.

    payload shape: ``{"kind": "stop|keyinfo|intervene", "body": "..."}``
    - stop: write `_stop` (empty) — the agent halts at next turn.
    - keyinfo: write `_keyinfo` with the body — the agent merges it into
      working memory's key_info.
    - intervene: write `_intervene` with the body — the agent prepends
      the body to its next user prompt.

    See GA ga.py:539-542. No-op (404) if no active agent for the ticket.
    """
    kind = (payload.get("kind") or "").strip()
    body = payload.get("body", "")
    if kind not in ("stop", "keyinfo", "intervene"):
        raise HTTPException(400, "kind must be one of: stop, keyinfo, intervene")
    targets = _resolve_active_task_dirs(identifier)
    if not targets:
        raise HTTPException(404, f"no active agent task dir for {identifier}")
    fname = f"_{kind}"
    written: list[str] = []
    for d in targets:
        try:
            with open(os.path.join(d, fname), "w", encoding="utf-8") as fh:
                fh.write(body if kind != "stop" else "")
            written.append(d)
        except Exception:
            continue
    return {"written": written, "kind": kind}


class _TicketAnswerBody(BaseModel):
    content: str = Field(..., min_length=1)


@router.post("/api/tickets/{identifier}/answer", responses={404: {"description": "Not found"}})
def ticket_answer(identifier: str, body: _TicketAnswerBody) -> dict:
    """Answer a clarification a chat/interactive ticket asked. Folds the
    answer into the ticket body, marks it clarified, and re-queues it so
    the pipeline resumes with the new context."""
    t = tickets_mod.get(identifier)
    if t is None:
        raise HTTPException(404, f"ticket {identifier} not found")
    ans = body.content.strip()
    tickets_mod.append_body(t.id, f"\n\n## Clarification\n{ans}\n")
    tickets_mod.add_comment(t.id, "user", ans)
    tickets_mod.add_event(t.id, "clarify", "clarification_answer", ans, {})
    tickets_mod.update_status(
        t.id, "todo", role="chat",
        metadata_patch={"clarified": True, "awaiting_input": False},
    )
    return {"ticket": t.identifier, "status": "todo",
            "trace_url": f"/api/tickets/{t.identifier}/events/stream"}


_TERMINAL_TICKET = {"done", "qa", "qa_failed", "cancelled"}


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload) + "\n\n"


def _event_payload(e: dict) -> dict:
    created = e.get("created_at")
    return {"kind": e.get("kind"), "agent_role": e.get("agent_role"),
            "body": e.get("body") or "", "metadata": e.get("metadata") or {},
            "created_at": (created.isoformat()
                           if hasattr(created, "isoformat") else created)}


def _new_events(tid, seen: set):
    for e in tickets_mod.comments(tid, 1000):
        eid = e.get("id")
        if eid not in seen:
            seen.add(eid)
            yield _sse(_event_payload(e))


def _terminal_line(t) -> str | None:
    """The closing ``done`` event, or None while the run continues."""
    if t.status in _TERMINAL_TICKET:
        return _sse({"kind": "done", "status": t.status})
    if t.status == "blocked":
        return _sse({"kind": "done", "status": "blocked"})
    return None


def _ticket_event_stream(identifier: str):
    import time as _t
    t0 = tickets_mod.get(identifier)
    if t0 is None:
        yield _sse({"kind": "error", "body": "ticket not found"})
        return
    seen: set = set()
    for _ in range(1200):   # ~40 min at 2s
        t = tickets_mod.get(identifier)
        if t is None:
            return
        yield from _new_events(t0.id, seen)
        meta = t.metadata or {}
        awaiting = bool(meta.get("awaiting_input"))
        yield _sse({"kind": "status", "status": t.status,
                    "awaiting_input": awaiting,
                    "clarify_questions": meta.get("clarify_questions") or []})
        if awaiting:
            return
        done = _terminal_line(t)
        if done:
            yield done
            return
        _t.sleep(2)


@router.get("/api/tickets/{identifier}/events/stream")
def stream_ticket_events(identifier: str) -> StreamingResponse:
    """Live stage updates for a ticket, sourced from ``ticket_events`` in
    the DB (shared across the api + runner containers — unlike the
    log-tail trace). Emits every event for the ticket, then polls for new
    ones; emits the clarification + status when the run pauses awaiting
    the user; closes on a terminal status. Chat Pipeline mode streams
    this."""
    return sse_response(_ticket_event_stream(identifier))
