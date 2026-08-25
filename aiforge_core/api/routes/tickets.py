"""Ticket routes (/api/tickets/*, /api/workflows/preview) — split out of api.py.

Full ticket CRUD (list/detail/create/patch/delete/reset), route override +
detector preview, comments, parallel-subtask kickoff, live agent intervention,
clarification answer, and the DB-sourced ticket-events SSE stream. Ticket row/
event shaping + attachment persist/remove helpers moved here VERBATIM; handlers
keep their inline function-local imports and behaviour.
"""
from __future__ import annotations

import glob as _glob_mod  # local alias to avoid leaking name
import json
import logging
import os
import threading
from datetime import UTC

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from aiforge_core.api.routes._sse import sse_response
from pydantic import BaseModel, Field

from aiforge_core.api._shared import _ticket_files_base
from aiforge_core.config import env as _cfg
from aiforge_core.runtime.background import spawn as _spawn
from aiforge_core.tickets import store as tickets_mod

router = APIRouter()

_af_log = logging.getLogger("aiforge")

_TERMINAL = {"done", "cancelled"}


def _as_utc(ts):
    from datetime import datetime
    if ts is None:
        return datetime.now(UTC)
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _duration_s(started, completed, status) -> float | None:
    """Seconds from start to completion — or to NOW while the run is still
    going. None until it has started."""
    if started is None:
        return None
    end = completed if (completed and status in _TERMINAL) else None
    return max(0.0, (_as_utc(end) - _as_utc(started)).total_seconds())


def _iso(ts):
    return ts.isoformat() if ts else None


def _ticket_row_out(r: dict) -> dict:
    started, completed = r.get("started_at"), r.get("completed_at")
    return {
        "id": r["id"], "identifier": r["identifier"], "title": r["title"],
        "body": r["body"], "status": r["status"], "priority": r["priority"],
        "assignee_role": (_cfg.canonical_role(r["assignee_role"])
                          if r.get("assignee_role") else None),
        "active_role": r.get("active_role"),
        "parent_id": r["parent_id"],
        "branch": r["branch"], "project": r["project"],
        "labels": list(r["labels"] or []),
        "metadata": dict(r["metadata"] or {}),
        "created_at": _iso(r.get("created_at")),
        "updated_at": _iso(r["updated_at"]),
        "completed_at": _iso(completed),
        "started_at": _iso(started),
        "duration_s": _duration_s(started, completed, r.get("status")),
        "route": r.get("route") or "code",
        "route_workflow": r.get("route_workflow"),
        "route_source": r.get("route_source") or "auto",
        "route_confidence": r.get("route_confidence"),
    }


def _event_row_out(r: dict) -> dict:
    return {
        "id": r["id"], "ticket_id": r["ticket_id"],
        "agent_role": r["agent_role"], "kind": r["kind"],
        "body": r["body"] or "",
        "metadata": dict(r["metadata"] or {}),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


# ─────────────────────────── Tickets ────────────────────────────────────
class AttachedFile(BaseModel):
    """File the operator dragged into the New Ticket form.

    Persisted to disk on ticket-create + recorded in
    ``ticket.metadata.attached_files`` so the runner can hand the paths
    to the Doer prompt. The runner materializes the files into the
    per-ticket worktree; the Doer reads them with its ``file_read`` tool.
    """

    name: str
    content_b64: str  # raw bytes, base64-encoded


class TicketCreate(BaseModel):
    title: str
    body: str = ""
    assignee_role: str | None = None
    priority: str = "medium"
    parent_identifier: str | None = None
    project: str | None = None
    labels: list[str] = Field(default_factory=list)
    max_turns: int | None = None
    metadata: dict | None = None
    # Route override — when set, skips auto-detection. Use this from
    # the UI when the human picks "Workflow" + workflow_id manually.
    route: str | None = None                    # 'code' | 'workflow' | None=auto
    route_workflow: str | None = None           # required when route='workflow'
    attachments: list[str] = Field(default_factory=list)  # attachment role names; feeds detector
    attached_files: list[AttachedFile] = Field(default_factory=list)
    # Deploy autonomy — operator opts the pipeline into auto-merge +
    # wait-for-deploy + live test on a real environment. 'none' (the
    # default) keeps the old PR-only flow.
    deploy_target: str | None = None            # 'none' | 'qa' | 'prod' | None


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


class TicketPatch(BaseModel):
    status: str | None = None
    assignee_role: str | None = None
    labels: list[str] | None = None
    body: str | None = None
    max_turns: int | None = None
    metadata: dict | None = None
    # Post-create attachment editing. New uploads (base64) are persisted
    # to the per-ticket dir; remove_files names are unlinked. The runner
    # materializes surviving attachments into the worktree for the Doer.
    attached_files: list[AttachedFile] = Field(default_factory=list)
    remove_files: list[str] = Field(default_factory=list)


class CommentCreate(BaseModel):
    body: str
    author: str = "human"


@router.get("/api/tickets")
def list_tickets(role: str | None = Query(None),
                 status: str | None = Query(None),
                 parent: str | None = Query(None),
                 limit: int = Query(100, le=500)) -> list[dict]:
    # Backend-agnostic (SQLite default / Postgres opt-in). `active_role`
    # = role of the most-recent agent event; `started_at` = first
    # in_progress event time — both enriched by the store layer.
    statuses = [s.strip() for s in status.split(",")] if status else None
    rows = tickets_mod.list_tickets(
        role=role, statuses=statuses, parent_identifier=parent, limit=limit,
    )
    return [_ticket_row_out(r) for r in rows]


@router.get("/api/tickets/{identifier}", responses={404: {"description": "Not found"}})
def get_ticket(identifier: str) -> dict:
    # Backend-agnostic ticket detail. Children are fetched via the
    # enriched list filtered by this ticket as parent.
    t = tickets_mod.get_enriched(identifier)
    if not t:
        raise HTTPException(404, f"ticket {identifier} not found")
    ticket_id = t["id"]
    events = [_event_row_out(r) for r in tickets_mod.comments(ticket_id, 500)]
    children = [
        _ticket_row_out(r)
        for r in tickets_mod.list_tickets(parent_identifier=identifier, limit=500)
    ]
    # Per-stage timeline — one row per agent that emitted a stage_done
    # event. Lets the UI render an inline timing breakdown without
    # parsing every event payload. Order = chronological.
    timings: list[dict] = []
    for ev in events:
        if ev.get("kind") != "stage_done":
            continue
        meta = ev.get("metadata") or {}
        timings.append({
            "stage": meta.get("stage") or ev.get("role"),
            "duration_s": meta.get("duration_s"),
            "at": ev.get("created_at"),
            "extra": {
                k: v for k, v in meta.items()
                if k not in ("stage", "duration_s")
            },
        })
    # Internal subtasks (Planner decomposition, event-sourced) + progress, so
    # the UI can chart the breakdown.
    try:
        from aiforge_core.tickets import subtasks as _subtasks
        _subs = _subtasks.get_subtasks(ticket_id)
        _subprog = _subtasks.progress(_subs)
    except Exception:  # noqa: BLE001
        _subs, _subprog = [], {"total": 0, "done": 0, "counts": {}, "fraction": 0.0}
    return {
        "ticket": _ticket_row_out(t),
        "events": events,
        "children": children,
        "timings": timings,
        "subtasks": _subs,
        "subtask_progress": _subprog,
    }


def _derive_branch(identifier: str, title: str) -> str:
    """Derive an `aiforge/<id>-<slug>` branch name from ticket id + title.

    Slug: lowercase title, non-alnum → `-`, collapse repeats, trim to 40
    chars. Doer's git-push step expects ticket.branch != None to push +
    open a PR. If the API caller didn't provide one, this fills it in.
    """
    import re as _re
    raw = (title or "").lower()
    slug = _re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:40].rstrip("-")
    return f"aiforge/{identifier}{('-' + slug) if slug else ''}"


def _persist_ticket_attachments(
    identifier: str, files: list[AttachedFile],
) -> list[dict]:
    """Decode + write each uploaded file under the persistent ticket-files
    base (see :func:`_ticket_files_base`); the runner later materializes them
    into the per-ticket worktree by absolute path. Returns a metadata-friendly
    list of ``{name, size, path, abs_path}`` — ``path`` is the worktree-view
    path the Doer prompt references; ``abs_path`` is the real persistent file.
    """
    import base64
    from pathlib import Path as _Path

    target_dir = _ticket_files_base() / identifier
    target_dir.mkdir(parents=True, exist_ok=True)

    out: list[dict] = []
    for f in files:
        # Defensive: strip directory components so a malicious name
        # like ``../../etc/passwd`` can't escape the per-ticket dir.
        safe_name = _Path(f.name).name or "attachment.bin"
        try:
            data = base64.b64decode(f.content_b64, validate=False)
        except Exception:
            continue
        dest = target_dir / safe_name
        dest.write_bytes(data)
        # Worktree-view path the Doer reads (the runner copies the file to
        # this same relative location inside the worktree). Decoupled from
        # the physical storage base so persistence can move without breaking
        # the prompt reference.
        rel = f".aiforge/ticket-files/{identifier}/{safe_name}"
        # ``abs_path`` is the real persistent location — valid even when the
        # runner rebinds AIFORGE_REPO_ROOT to a per-ticket worktree.
        out.append({
            "name": safe_name, "size": len(data),
            "path": rel, "abs_path": str(dest),
        })
    return out


def _remove_ticket_attachments(
    identifier: str, names: list[str],
) -> list[str]:
    """Delete named files from a ticket's attachment dir.

    Mirrors ``_persist_ticket_attachments`` path resolution (the shared
    persistent base). Each name is reduced to its basename (``../`` traversal
    stripped) before unlinking ``{base}/{id}/<name>``. A missing file is a
    no-op. Returns the basenames actually removed.
    """
    from pathlib import Path as _Path

    target_dir = _ticket_files_base() / identifier

    removed: list[str] = []
    for n in names:
        safe_name = _Path(n).name
        if not safe_name:
            continue
        dest = target_dir / safe_name
        try:
            if dest.exists():
                dest.unlink()
                removed.append(safe_name)
        except OSError:
            continue
    return removed


def _parent_id_of(parent_identifier) -> int | None:
    if not parent_identifier:
        return None
    parent = tickets_mod.get(parent_identifier)
    if parent is None:
        raise HTTPException(400, f"parent {parent_identifier} not found")
    return parent.id


def _deploy_target(raw) -> str:
    """Normalize to one of {none, qa, prod}; anything else is treated as 'none'
    so a typo can't accidentally arm an autonomous merge."""
    dt = (raw or "none").lower().strip()
    return dt if dt in {"none", "qa", "prod"} else "none"


def _resolve_route(payload, md: dict) -> tuple:
    """``(route, workflow, source, confidence)``.

    The UI may pin route+workflow manually OR ask the detector to pick. Manual
    choices flag route_source='manual' so audits stay clean; auto picks set
    'auto'. The detector must never break a ticket POST.
    """
    if payload.route in ("code", "workflow"):
        if payload.route == "workflow" and not payload.route_workflow:
            raise HTTPException(
                400, "route='workflow' requires route_workflow id")
        return payload.route, payload.route_workflow, "manual", 1.0
    try:
        from aiforge_core.workflows import detect_route
        decided = detect_route(title=payload.title, body=payload.body,
                               attachments=payload.attachments, intent=None)
        md["route_rationale"] = decided.rationale
        return decided.kind, decided.workflow_id, "auto", decided.confidence
    except Exception as exc:  # noqa: BLE001
        md["route_error"] = str(exc)[:300]
        return "code", None, "auto", None


def _attach_files(t, attached_files) -> None:
    """Persist uploaded files into a per-ticket dir under the workspace and
    stamp metadata.attached_files. The runner materializes them into the
    per-ticket worktree so the Doer can ``file_read`` them on whatever provider
    the role is configured for."""
    if not attached_files:
        return
    attach_meta = _persist_ticket_attachments(t.identifier, attached_files)
    if not attach_meta:
        return
    patched_md = dict(t.metadata or {})
    patched_md["attached_files"] = attach_meta
    try:
        tickets_mod.update_status(t.id, t.status, role="api",
                                  metadata_patch={"attached_files": attach_meta})
        t.metadata = patched_md
    except Exception:  # noqa: BLE001
        pass


def _ensure_branch(t) -> None:
    if t.branch:
        return
    t.branch = _derive_branch(t.identifier, t.title)
    try:
        tickets_mod.set_branch(t.id, t.branch)
    except Exception:  # noqa: BLE001
        pass


def _ticket_out(t) -> dict:
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


@router.post("/api/tickets")
def create_ticket(payload: TicketCreate) -> dict:
    parent_id = _parent_id_of(payload.parent_identifier)
    md = dict(payload.metadata or {})
    if payload.max_turns is not None:
        md["max_turns"] = int(payload.max_turns)
    md["deploy_target"] = _deploy_target(payload.deploy_target)
    assignee = (_cfg.canonical_role(payload.assignee_role)
                if payload.assignee_role else None)
    route, route_workflow, route_source, route_confidence = _resolve_route(
        payload, md)
    # IntentLayer enrichment was the legacy path. The new aiforge_agents
    # Understander does its own grounding via AiForgeMemory at run-time, so we
    # no longer pre-enrich on ticket create: tickets store body + title only,
    # and the agent adds context_md + understanding when the run starts.
    t = tickets_mod.create(
        title=payload.title, body=payload.body, assignee_role=assignee,
        priority=payload.priority, parent_id=parent_id,
        project=payload.project, labels=payload.labels, metadata=md or None,
        route=route, route_workflow=route_workflow,
        route_source=route_source, route_confidence=route_confidence)
    _attach_files(t, payload.attached_files)
    _ensure_branch(t)
    return _ticket_out(t)


def _edited_attachments(t, payload) -> list:
    """Remove first, then add, then hand back the recomputed list. jsonb '||'
    shallow-merge replaces the whole attached_files key, so passing the FULL
    list is what covers add + remove."""
    current = list((t.metadata or {}).get("attached_files") or [])
    if payload.remove_files:
        removed = set(_remove_ticket_attachments(t.identifier,
                                                 payload.remove_files))
        current = [f for f in current
                   if (f.get("name") if isinstance(f, dict) else None)
                   not in removed]
    if payload.attached_files:
        current.extend(_persist_ticket_attachments(t.identifier,
                                                   payload.attached_files))
    return current


def _patch_metadata(t, payload) -> dict:
    md: dict = dict(payload.metadata or {})
    if payload.max_turns is not None:
        md["max_turns"] = int(payload.max_turns)
    if payload.remove_files or payload.attached_files:
        md["attached_files"] = _edited_attachments(t, payload)
    return md


def _patch_fields(payload) -> dict:
    fields: dict = {}
    if payload.assignee_role:
        fields["assignee_role"] = _cfg.canonical_role(payload.assignee_role)
    if payload.labels is not None:
        fields["labels"] = payload.labels
    if payload.body is not None:
        fields["body"] = payload.body
    return fields


@router.patch("/api/tickets/{identifier}", responses={400: {"description": "Bad request"}, 404: {"description": "Not found"}})
def patch_ticket(identifier: str, payload: TicketPatch) -> dict:
    t = tickets_mod.get(identifier)
    if t is None:
        raise HTTPException(404, f"ticket {identifier} not found")
    if payload.status:
        if payload.status not in tickets_mod.VALID_STATUS:
            raise HTTPException(400, f"bad status {payload.status!r}")
        tickets_mod.update_status(t.id, payload.status, role="human")
    merge_md = _patch_metadata(t, payload)
    fields = _patch_fields(payload)
    if fields or merge_md:
        # Backend-agnostic update (the old raw Postgres SQL — COALESCE/jsonb —
        # broke in SQLite/--lite mode).
        tickets_mod.patch_fields(t.id, fields=fields, metadata_patch=merge_md)
    return get_ticket(identifier)


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


@router.post("/api/tickets/{identifier}/run-parallel", status_code=202, responses={404: {"description": "Not found"}})
def run_subtasks_parallel(identifier: str) -> dict:
    """Run this ticket's subtasks CONCURRENTLY (each in its own worktree),
    merging successful branches back. Runs in the background; the ticket moves
    todo → in_progress → done and the subtask chart updates live. A fresh ticket
    with no subtasks is decomposed first."""
    t = tickets_mod.get(identifier)
    if t is None:
        raise HTTPException(404, f"ticket {identifier} not found")
    import threading

    def _bg():
        try:
            from aiforge_core.runtime.parallel_subtasks import run_subtasks_parallel as _run
            _run(t)
        except Exception as exc:  # noqa: BLE001
            _af_log.warning("run-parallel failed for %s: %s", identifier, exc)

    _spawn(_bg, name=f"parallel-{identifier}")
    return {"started": True, "identifier": identifier}


@router.post("/api/tickets/{identifier}/comments", status_code=201, responses={404: {"description": "Not found"}})
def add_comment(identifier: str, payload: CommentCreate) -> dict:
    t = tickets_mod.get(identifier)
    if t is None:
        raise HTTPException(404, f"ticket {identifier} not found")
    eid = tickets_mod.add_comment(t.id, payload.author, payload.body)
    return {"event_id": eid}


@router.post("/api/tickets/reset")
def tickets_reset() -> dict:
    """Delete ALL tickets + events and reset the ONE-<n> counter so the next
    ticket restarts the sequence. Worktrees / branches / PRs are NOT touched."""
    return {"ok": True, "deleted": tickets_mod.reset_all()}


@router.delete("/api/tickets/{identifier}", status_code=204, responses={404: {"description": "Not found"}})
def delete_ticket(identifier: str) -> None:
    """Delete a ticket and its events. Worktree, branch, and any open PR
    are deliberately NOT touched — operator handles those out-of-band
    so a typo doesn't nuke pushed code. Returns 404 if no such ticket."""
    # Routed through the store/backend so it works on BOTH the embedded
    # SQLite and the Postgres backend (the old raw _db() path 500'd on
    # SQLite). 404 if no such ticket.
    if not tickets_mod.delete(identifier):
        raise HTTPException(404, f"ticket {identifier} not found")
    return None


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
    for root in ga_root_candidates:
        if root and os.path.isdir(root):
            base = os.path.join(root, "temp")
            return sorted(_glob_mod.glob(
                os.path.join(base, f"aiforge-{identifier}-*")
            )) + sorted(_glob_mod.glob(
                os.path.join(base, f"aiforge-planner-{identifier}-*")
            ))
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
