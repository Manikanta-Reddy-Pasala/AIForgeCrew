"""FastAPI backend for the dashboard UI.

Exposes the aiforge Postgres state + live log tails as a small REST + SSE
surface the React/Vite frontend talks to.

Run:
    uvicorn aiforge_core.api.api:app --host 127.0.0.1 --port 8799 --reload

Routes:
    GET  /api/health
    GET  /api/agents
    GET  /api/tickets                     # ?role=&status=&parent=&limit=
    GET  /api/tickets/{identifier}        # incl. events + children + git
    POST /api/tickets                     # create
    PATCH /api/tickets/{id}               # status / labels / assignee
    POST /api/tickets/{id}/comments
    GET  /api/logs/{role}/stream          # SSE live tail of orchestrator ndjson
    GET  /api/memory/stats
    GET  /api/memory/search?q=&wing=&top_k=
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

import psycopg
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from aiforge_core.config import env as _cfg
from aiforge_core.config.env import (
    AIFORGE_DSN,
    LM_STUDIO_BASE_URL,
    LOG_DIR,
    ROLES,
)
from aiforge_core.tickets import store as tickets_mod

app = FastAPI(title="AIForge API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev only
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────── Boot-time wiring ───────────────────────────
# OpenTelemetry — no-op when AIFORGE_OTEL_ENABLED != "1" (see otel.py).
# Initialised once at module load so every request inherits the tracer.
try:
    from aiforge_core.observability import otel as _otel
    _otel.setup()
except Exception as _exc:
    print(f"[boot] otel setup skipped: {_exc}")


# ─────────────────────────── Helpers ────────────────────────────────────
def _db():
    return psycopg.connect(AIFORGE_DSN, row_factory=dict_row, connect_timeout=5,
                           options="-c statement_timeout=10000")


_TERMINAL = {"done", "cancelled"}


def _ticket_row_out(r: dict) -> dict:
    started = r.get("started_at")
    completed = r.get("completed_at")
    created = r.get("created_at")
    status = r.get("status")
    end = completed if (completed and status in _TERMINAL) else None
    if started is None:
        duration_s: float | None = None
    else:
        from datetime import datetime, timezone
        end_ts = end or datetime.now(timezone.utc)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if end_ts.tzinfo is None:
            end_ts = end_ts.replace(tzinfo=timezone.utc)
        duration_s = max(0.0, (end_ts - started).total_seconds())
    active_role = r.get("active_role")
    return {
        "id": r["id"], "identifier": r["identifier"], "title": r["title"],
        "body": r["body"], "status": r["status"], "priority": r["priority"],
        "assignee_role": _cfg.canonical_role(r["assignee_role"]) if r.get("assignee_role") else None,
        "active_role": active_role,
        "parent_id": r["parent_id"],
        "branch": r["branch"], "project": r["project"],
        "labels": list(r["labels"] or []),
        "metadata": dict(r["metadata"] or {}),
        "created_at": created.isoformat() if created else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        "completed_at": completed.isoformat() if completed else None,
        "started_at": started.isoformat() if started else None,
        "duration_s": duration_s,
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


# ─────────────────────────── Health / Agents ────────────────────────────
@app.get("/api/health")
def health() -> dict:
    from aiforge_core.tickets.backend_factory import get_backend
    status = {"ok": True, "postgres": False, "storage": None, "lm_studio": False}
    try:
        be = get_backend()
        status["storage"] = be.name
        # Cheap reachability probe — an identifier that never exists.
        tickets_mod.get("__healthcheck__")
        status["postgres"] = be.name == "postgres"
    except Exception:
        status["ok"] = False
    try:
        import urllib.request

        from aiforge_core.net.ssl import context_for as _ssl_context_for
        _lm_url = f"{LM_STUDIO_BASE_URL}/models"
        with urllib.request.urlopen(
            _lm_url, timeout=3, context=_ssl_context_for(_lm_url)) as r:
            status["lm_studio"] = r.getcode() == 200
    except Exception:
        pass
    return status


@app.get("/api/agents")
def list_agents() -> list[dict]:
    """Static role catalogue + dynamic last-activity from ticket_events."""
    out = []
    # Activity stats use Postgres-specific SQL (FILTER). On the embedded
    # SQLite backend they degrade to nulls — the static role catalogue
    # still renders so the Agents / Home views work everywhere.
    def _activity(name: str) -> tuple:
        try:
            with _db() as c, c.cursor() as cur:
                cur.execute(
                    "SELECT MAX(created_at) AS last_activity, "
                    "COUNT(*) FILTER (WHERE kind='llm_turn') AS turns "
                    "FROM ticket_events WHERE agent_role = %s",
                    (name,),
                )
                row = cur.fetchone() or {}
                cur.execute(
                    "SELECT identifier, status FROM tickets "
                    "WHERE assignee_role = %s AND status IN "
                    "('todo','in_progress','in_review') ORDER BY created_at DESC",
                    (name,),
                )
                active = [{"identifier": r["identifier"], "status": r["status"]}
                          for r in cur.fetchall()]
            last = row.get("last_activity")
            return (last.isoformat() if last else None, row.get("turns", 0), active)
        except Exception:
            return (None, 0, [])

    for name, rc in ROLES.items():
        last_iso, turns, active = _activity(name)
        out.append({
            "role": name,
            "model": rc.model,
            "transport": rc.transport,
            "max_turns": rc.max_turns,
            "tool_allowlist": list(rc.tool_allowlist),
            "last_activity": last_iso,
            "lifetime_turns": turns,
            "active_tickets": active,
        })
    return out


# ─────────────────────────── Tickets ────────────────────────────────────
class AttachedFile(BaseModel):
    """File the operator dragged into the New Ticket form.

    Persisted to disk on ticket-create + recorded in
    ``ticket.metadata.attached_files`` so the runner can hand the paths
    to the Doer prompt. Triggers a per-ticket override that pins the
    pipeline to ``claude_local`` — only Claude's subscription CLI can
    read attachments via its native filesystem tools, the LiteLLM /
    Ollama / Anthropic-API providers don't have a way to inline them
    short of base64-stuffing the prompt.
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
    # to the per-ticket dir; remove_files names are unlinked. Presence of
    # any surviving attachment forces claude_local (only the CLI can read
    # arbitrary files inline), matching create-time behavior.
    attached_files: list[AttachedFile] = Field(default_factory=list)
    remove_files: list[str] = Field(default_factory=list)


class CommentCreate(BaseModel):
    body: str
    author: str = "human"


@app.get("/api/tickets")
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


@app.get("/api/tickets/{identifier}")
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
    return {
        "ticket": _ticket_row_out(t),
        "events": events,
        "children": children,
        "timings": timings,
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
    """Decode + write each uploaded file under the workspace.

    Files land at ``{AIFORGE_REPO_ROOT}/.aiforge/ticket-files/{id}/<name>``
    so claude_local's ``--add-dir <root>`` whitelist already covers
    them. Returns a metadata-friendly list of ``{name, size, path}`` —
    path is relative to the repo root so the Doer prompt can reference
    it without leaking absolute filesystem layout.
    """
    import base64
    import os as _os
    from pathlib import Path as _Path

    root = _Path(_os.path.expanduser(_os.environ.get(
        "AIFORGE_REPO_ROOT", "~/aiforge_workspace",
    ))).resolve()
    target_dir = root / ".aiforge" / "ticket-files" / identifier
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
        rel = dest.relative_to(root).as_posix()
        # ``abs_path`` stays valid even when downstream code (the
        # runner) rebinds AIFORGE_REPO_ROOT to a per-ticket worktree,
        # so the materializer can locate the upload from anywhere.
        out.append({
            "name": safe_name, "size": len(data),
            "path": rel, "abs_path": str(dest),
        })
    return out


def _remove_ticket_attachments(
    identifier: str, names: list[str],
) -> list[str]:
    """Delete named files from a ticket's attachment dir.

    Mirrors ``_persist_ticket_attachments`` path resolution. Each name
    is reduced to its basename (``../`` traversal stripped) before
    unlinking ``{root}/.aiforge/ticket-files/{id}/<name>``. A missing
    file is a no-op. Returns the basenames actually removed.
    """
    import os as _os
    from pathlib import Path as _Path

    root = _Path(_os.path.expanduser(_os.environ.get(
        "AIFORGE_REPO_ROOT", "~/aiforge_workspace",
    ))).resolve()
    target_dir = root / ".aiforge" / "ticket-files" / identifier

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


@app.post("/api/tickets", status_code=201)
def create_ticket(payload: TicketCreate) -> dict:
    parent_id = None
    if payload.parent_identifier:
        parent = tickets_mod.get(payload.parent_identifier)
        if parent is None:
            raise HTTPException(400, f"parent {payload.parent_identifier} not found")
        parent_id = parent.id
    md = dict(payload.metadata or {})
    if payload.max_turns is not None:
        md["max_turns"] = int(payload.max_turns)
    # Deploy target — normalize to one of {none, qa, prod}; anything
    # else is treated as 'none' so a typo can't accidentally arm an
    # autonomous merge.
    dt = (payload.deploy_target or "none").lower().strip()
    if dt not in {"none", "qa", "prod"}:
        dt = "none"
    md["deploy_target"] = dt
    assignee = _cfg.canonical_role(payload.assignee_role) if payload.assignee_role else None
    # IntentLayer — translate plain language at INGRESS so every
    # downstream agent (planner, doer) sees enriched body + metadata.
    # AIFORGE_INTENT_ENRICH=0 disables (offline / debugging).
    # IntentLayer enrichment was the legacy path. The new
    # aiforge_agents Understander does its own grounding via
    # AiForgeMemory at run-time, so we no longer pre-enrich on
    # ticket create. Tickets store body + title only; the agent
    # adds context_md + understanding when the run starts.
    enriched_body = payload.body
    enrichment_meta: dict = {}
    # Project resolution priority: explicit POST field > UC-resolved
    # repo > intent.repo_hint. Was: explicit > repo_hint only (missed
    # the body-text repo resolver entirely so PosClientBackend
    # fallback fired).
    resolved_project = (
        payload.project
        or enrichment_meta.get("repo")
        or enrichment_meta.get("intent", {}).get("repo_hint")
    )

    # Route resolution. UI may pin route+workflow manually OR ask the
    # detector to pick. Manual choices flag route_source='manual' so
    # audits stay clean. Auto picks set route_source='auto'.
    route = "code"
    route_workflow: str | None = None
    route_source = "auto"
    route_confidence: float | None = None
    if payload.route in ("code", "workflow"):
        route = payload.route
        route_workflow = payload.route_workflow
        route_source = "manual"
        route_confidence = 1.0
        if route == "workflow" and not route_workflow:
            raise HTTPException(
                400, "route='workflow' requires route_workflow id",
            )
    else:
        try:
            from aiforge_core.workflows import detect_route
            decided = detect_route(
                title=payload.title, body=payload.body,
                attachments=payload.attachments,
                intent=enrichment_meta.get("intent"),
            )
            route = decided.kind
            route_workflow = decided.workflow_id
            route_confidence = decided.confidence
            md["route_rationale"] = decided.rationale
        except Exception as exc:  # detector must never break ticket POST
            md["route_error"] = str(exc)[:300]

    t = tickets_mod.create(
        title=payload.title, body=enriched_body,
        assignee_role=assignee,
        priority=payload.priority, parent_id=parent_id,
        project=resolved_project,
        labels=payload.labels,
        metadata=md or None,
        route=route, route_workflow=route_workflow,
        route_source=route_source, route_confidence=route_confidence,
    )
    # Persist any uploaded files into a per-ticket dir under the
    # workspace and stamp metadata.attached_files. Force claude_local
    # for the run because attachments are only readable through the
    # subscription CLI's native filesystem tools (LiteLLM/Ollama
    # providers can't ingest arbitrary files inline). The metadata
    # flag is read by the runtime in
    # ``aiforge_core.runtime.pipeline.build_litellm_model``.
    if payload.attached_files:
        attach_meta = _persist_ticket_attachments(t.identifier,
                                                  payload.attached_files)
        if attach_meta:
            patched_md = dict(t.metadata or {})
            patched_md["attached_files"] = attach_meta
            patched_md["force_provider"] = "claude_local"
            try:
                tickets_mod.update_status(
                    t.id, t.status, role="api",
                    metadata_patch={
                        "attached_files": attach_meta,
                        "force_provider": "claude_local",
                    },
                )
                t.metadata = patched_md
            except Exception:
                pass
    if not t.branch:
        t.branch = _derive_branch(t.identifier, t.title)
        try:
            tickets_mod.set_branch(t.id, t.branch)
        except Exception:
            pass
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


@app.patch("/api/tickets/{identifier}")
def patch_ticket(identifier: str, payload: TicketPatch) -> dict:
    t = tickets_mod.get(identifier)
    if t is None:
        raise HTTPException(404, f"ticket {identifier} not found")
    if payload.status:
        if payload.status not in tickets_mod.VALID_STATUS:
            raise HTTPException(400, f"bad status {payload.status!r}")
        tickets_mod.update_status(t.id, payload.status, role="human")
    merge_md: dict = {}
    if payload.metadata:
        merge_md.update(payload.metadata)
    if payload.max_turns is not None:
        merge_md["max_turns"] = int(payload.max_turns)
    # Attachment editing: remove first, then add, then stamp the
    # recomputed list. jsonb '||' shallow-merge replaces the whole
    # attached_files key — so passing the full list covers add + remove.
    if payload.remove_files or payload.attached_files:
        current = list((t.metadata or {}).get("attached_files") or [])
        if payload.remove_files:
            removed = set(_remove_ticket_attachments(
                t.identifier, payload.remove_files))
            current = [
                f for f in current
                if (f.get("name") if isinstance(f, dict) else None)
                not in removed
            ]
        if payload.attached_files:
            current.extend(_persist_ticket_attachments(
                t.identifier, payload.attached_files))
        merge_md["attached_files"] = current
        # Force claude_local while files remain; clear the flag when the
        # last attachment is removed so the run can use any provider.
        merge_md["force_provider"] = "claude_local" if current else None
    if (payload.assignee_role or payload.labels is not None
            or payload.body is not None or merge_md):
        sets: list[str] = []
        params: list[Any] = []
        if payload.assignee_role:
            sets.append("assignee_role=%s")
            params.append(_cfg.canonical_role(payload.assignee_role))
        if payload.labels is not None:
            sets.append("labels=%s"); params.append(payload.labels)
        if payload.body is not None:
            sets.append("body=%s"); params.append(payload.body)
        if merge_md:
            import json as _json
            sets.append("metadata = COALESCE(metadata,'{}'::jsonb) || %s::jsonb")
            params.append(_json.dumps(merge_md))
        params.append(t.id)
        with _db() as c, c.cursor() as cur:
            cur.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id=%s",
                        params)
            c.commit()
    return get_ticket(identifier)


@app.get("/api/workflows")
def list_workflows() -> list[dict]:
    """Public registry view — UI uses this to populate the workflow
    dropdown on the new-ticket form."""
    from aiforge_core.workflows import list_all
    return [w.to_public_dict() for w in list_all()]


@app.post("/api/workflows/preview")
def workflow_preview(payload: RoutePreview) -> dict:
    """Run the route detector against a candidate ticket WITHOUT
    creating it. UI debounces this on body change to show the
    detected workflow chip live."""
    from aiforge_core.workflows.detector import preview
    return preview(
        body=payload.body, title=payload.title,
        attachments=payload.attachments, intent=payload.intent,
    )


@app.put("/api/tickets/{identifier}/route")
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


@app.post("/api/tickets/{identifier}/comments", status_code=201)
def add_comment(identifier: str, payload: CommentCreate) -> dict:
    t = tickets_mod.get(identifier)
    if t is None:
        raise HTTPException(404, f"ticket {identifier} not found")
    eid = tickets_mod.add_comment(t.id, payload.author, payload.body)
    return {"event_id": eid}


@app.delete("/api/tickets/{identifier}", status_code=204)
def delete_ticket(identifier: str) -> None:
    """Delete a ticket and its events. Worktree, branch, and any open PR
    are deliberately NOT touched — operator handles those out-of-band
    so a typo doesn't nuke pushed code. Returns 404 if no such ticket."""
    t = tickets_mod.get(identifier)
    if t is None:
        raise HTTPException(404, f"ticket {identifier} not found")
    with _db() as c, c.cursor() as cur:
        cur.execute("DELETE FROM ticket_events WHERE ticket_id=%s", (t.id,))
        cur.execute("DELETE FROM tickets WHERE id=%s", (t.id,))
        c.commit()
    return None


# ─────────── Live agent intervention (GA _stop / _keyinfo / _intervene) ────
# Uses GA's task-intervention mechanism (commit 62ac73c). Harness writes
# control files into the running agent's task_dir; GA's turn_end_callback
# polls them and applies. Lets us steer or stop a live agent without
# restarting the runtime.
import glob as _glob_mod  # local alias to avoid leaking name


def _resolve_active_task_dirs(identifier: str) -> list[str]:
    """Return GA temp dirs that match a running agent for this ticket."""
    ga_root_candidates = (
        os.environ.get("AIFORGE_GA_DIR", ""),
        "/home/mani/genericagent",
        "/Users/manikanta/genericagent",
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


@app.post("/api/tickets/{identifier}/intervene")
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


_RUNTIME_ENV_PATH = os.path.expanduser(
    os.environ.get("AIFORGE_RUNTIME_ENV", "~/.aiforge/runtime.env")
)


def _persist_env(key: str, value: str) -> None:
    """Upsert ``key=value`` into runtime.env so graph-runner picks it up
    on next poll-cycle restart. KISS: line-replace; preserves order."""
    if not os.path.isfile(_RUNTIME_ENV_PATH):
        return
    lines = open(_RUNTIME_ENV_PATH).read().splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    with open(_RUNTIME_ENV_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")


@app.get("/api/runtime/token_usage")
def token_usage(ticket: str | None = None) -> dict:
    """Token totals per role per ticket — empty under new aiforge_agents
    pipeline (the legacy GA tokens module was removed). Token tracking
    will be re-added on the new orchestrator's audit path.
    """
    return {"all": {}, "per_ticket": {}}


@app.get("/api/runtime/rate_limits")
def get_rate_limits() -> dict:
    """Active rate-limit config + bucket state per provider.

    UI uses this to render bucket gauges and the limit-edit form.
    """
    from aiforge_core.llm import list_providers as _list
    from aiforge_core.llm import providers as _providers
    from aiforge_core.llm import rl_state as _state
    out: list[dict] = []
    for entry in _list():
        name = entry["name"]
        prov = _providers.get(name)
        declared = prov.rate_limits() if prov is not None else None
        rpm_env = os.environ.get(f"AIFORGE_{name.upper()}_RPM")
        tpm_env = os.environ.get(f"AIFORGE_{name.upper()}_TPM")
        rec = {
            "provider": name,
            "available": entry["available"],
            "declared": declared,
            "effective_rpm": float(rpm_env) if rpm_env else (declared or {}).get("rpm", 0),
            "effective_tpm": float(tpm_env) if tpm_env else (declared or {}).get("tpm", 0),
            "env_override_rpm": rpm_env,
            "env_override_tpm": tpm_env,
            "state": _state(name),
        }
        out.append(rec)
    return {"providers": out, "max_wait_s": int(os.environ.get("AIFORGE_LLM_MAX_WAIT_S", 120))}


@app.put("/api/runtime/rate_limits")
def set_rate_limit(payload: dict) -> dict:
    """Tighten/loosen a provider's RPM or TPM at runtime.

    payload: ``{"provider": "gemini", "rpm": 30, "tpm": 500000}``.
    Either field optional; sets ``AIFORGE_<PROVIDER>_RPM/_TPM`` env
    + persists to runtime.env.
    """
    provider = (payload.get("provider") or "").strip().lower()
    if not provider:
        raise HTTPException(400, "provider required")
    written: dict = {}
    for key in ("rpm", "tpm"):
        v = payload.get(key)
        if v is None:
            continue
        env_name = f"AIFORGE_{provider.upper()}_{key.upper()}"
        os.environ[env_name] = str(v)
        _persist_env(env_name, str(v))
        written[key] = v
    return {"provider": provider, "set": written}


@app.get("/api/runtime/llm_backend")
def get_llm_backend() -> dict:
    """Active LLM backend for all agents + the provider registry."""
    from aiforge_core.llm import list_providers as _list
    providers = _list()
    avail_names = [p["name"] for p in providers if p["available"]]
    value = (
        os.environ.get("AIFORGE_PRIMARY_BACKEND")
        or os.environ.get("AIFORGE_DOER_PRIMARY_BACKEND")
        or "local"
    ).lower()
    if value not in avail_names:
        value = "local"
    return {
        "backend": value,
        "options": avail_names,
        "providers": providers,
        # Legacy field for old UI builds; same as 'gemini' in options.
        "gemini_available": "gemini" in avail_names,
    }


@app.put("/api/runtime/llm_backend")
def set_llm_backend(payload: dict) -> dict:
    """Flip the active LLM backend for every agent.

    Affects runs started AFTER this call. graph-runner picks up the
    new value next poll-cycle restart (~10-15s).
    """
    from aiforge_core.llm import list_providers as _list
    avail = {p["name"] for p in _list() if p["available"]}
    backend = (payload.get("backend") or "").strip().lower()
    if backend not in avail:
        raise HTTPException(
            400, f"backend must be one of {sorted(avail)}; got {backend!r}"
        )
    os.environ["AIFORGE_PRIMARY_BACKEND"] = backend
    _persist_env("AIFORGE_PRIMARY_BACKEND", backend)
    # Drop the legacy doer-only key so it doesn't shadow the global flag.
    os.environ.pop("AIFORGE_DOER_PRIMARY_BACKEND", None)
    return {"backend": backend, "persisted": True}


# Legacy-compat aliases — keep older callers working until UI ships.
@app.get("/api/runtime/doer_backend")
def get_doer_backend_alias() -> dict:
    return get_llm_backend()


@app.put("/api/runtime/doer_backend")
def set_doer_backend_alias(payload: dict) -> dict:
    return set_llm_backend(payload)


@app.post("/api/runtime/session_param")
def session_param(payload: dict) -> dict:
    """Per-role LLM param tuning at runtime (GA /session.key=value, commit
    127a4e6). Updates the agent_config so the NEXT agent run picks new
    values. Doesn't affect a currently-running agent.

    payload: ``{"role": "doer|planner|...", "key": "temperature|max_tokens|...", "value": "..."}``
    """
    role = (payload.get("role") or "").strip()
    key = (payload.get("key") or "").strip()
    value = payload.get("value")
    if not role or not key or value is None:
        raise HTTPException(400, "role, key, value required")
    env_var = f"AIFORGE_{role.upper()}_{key.upper()}"
    os.environ[env_var] = str(value)
    return {"set": env_var, "value": str(value)}


# ─────────────────────────── Metrics ────────────────────────────────────
@app.get("/api/metrics")
def metrics() -> dict:
    """Operational metrics: ticket counts, verdict ratios, tick stop_reasons,
    memory hit-rate. Computed on-demand from aiforge Postgres."""
    with _db() as c, c.cursor() as cur:
        # tickets per status per role
        cur.execute(
            "SELECT assignee_role, status, COUNT(*) AS n "
            "FROM tickets GROUP BY assignee_role, status"
        )
        ticket_grid = [dict(r) for r in cur.fetchall()]

        # feedback verdict ratio (from metadata)
        cur.execute(
            "SELECT "
            " COUNT(*) FILTER (WHERE metadata->>'feedback_verdict'='pass') AS pass,"
            " COUNT(*) FILTER (WHERE metadata->>'feedback_verdict'='fail') AS fail,"
            " COUNT(*) FILTER (WHERE metadata->>'feedback_verdict'='implicit_pass') AS implicit_pass "
            "FROM tickets"
        )
        v = cur.fetchone() or {}

        # stop_reason distribution per role
        cur.execute(
            "SELECT assignee_role, metadata->>'last_stop_reason' AS stop_reason, "
            "COUNT(*) AS n FROM tickets "
            "WHERE metadata->>'last_stop_reason' IS NOT NULL "
            "GROUP BY assignee_role, metadata->>'last_stop_reason'"
        )
        stop_reasons = [dict(r) for r in cur.fetchall()]

        # reclaim distribution
        cur.execute(
            "SELECT COALESCE((metadata->>'reclaim_count')::int, 0) AS rc, "
            "COUNT(*) AS n FROM tickets "
            "WHERE (metadata->>'reclaim_count')::int > 0 "
            "GROUP BY rc ORDER BY rc"
        )
        reclaims = [dict(r) for r in cur.fetchall()]

        # Memory: hit-rate per tier/wing (A + B tracking)
        cur.execute(
            "SELECT tier, "
            " COUNT(*) AS total, "
            " COUNT(*) FILTER (WHERE (metadata->>'hit_count')::int > 0) AS hit, "
            " COUNT(*) FILTER (WHERE wing LIKE 'archived/%') AS archived "
            "FROM memories GROUP BY tier ORDER BY tier"
        )
        memory_hit = [dict(r) for r in cur.fetchall()]

        # Top-hit facts
        cur.execute(
            "SELECT id, tier, wing, source, LEFT(text, 120) AS text, "
            "COALESCE((metadata->>'hit_count')::int, 0) AS hits "
            "FROM memories "
            "WHERE tier IN ('t2', 't3') "
            "AND COALESCE((metadata->>'hit_count')::int, 0) > 0 "
            "ORDER BY (metadata->>'hit_count')::int DESC NULLS LAST LIMIT 10"
        )
        top_facts = [dict(r) for r in cur.fetchall()]

        # Ticks — avg duration + count per role (last 24h)
        cur.execute(
            "SELECT agent_role, COUNT(*) AS ticks "
            "FROM ticket_events WHERE kind='llm_turn' "
            "AND created_at > now() - interval '24 hours' "
            "GROUP BY agent_role"
        )
        activity_24h = [dict(r) for r in cur.fetchall()]

    return {
        "ticket_grid": ticket_grid,
        "feedback_verdicts": {
            "pass": v.get("pass", 0),
            "fail": v.get("fail", 0),
            "implicit_pass": v.get("implicit_pass", 0),
        },
        "stop_reasons": stop_reasons,
        "reclaim_distribution": reclaims,
        "memory_by_tier": memory_hit,
        "top_facts_by_hits": top_facts,
        "activity_24h": activity_24h,
    }


# ─────────────────────────── Memory ─────────────────────────────────────
def _neo4j_stats() -> dict:
    """Node counts per label from the graph (one row per label, plus a
    grand total). Soft — returns zeros on any driver error."""
    try:
        from neo4j import GraphDatabase

        from aiforge_core.memory.neo4j_conn import neo4j_params
        uri, user, pw = neo4j_params()
        drv = GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001
        return {"backend": "neo4j", "total": 0, "wings": [], "error": str(exc)}
    try:
        with drv.session() as s:
            total = s.run("MATCH (n) RETURN count(n) AS n").single()["n"]
            rows = s.run(
                "MATCH (n) UNWIND labels(n) AS label "
                "RETURN label, count(*) AS n ORDER BY n DESC LIMIT 30"
            )
            wings = [
                {"tier": "graph", "wing": r["label"], "n": r["n"], "embedded": r["n"]}
                for r in rows
            ]
        return {"backend": "neo4j", "total": int(total), "wings": wings}
    except Exception as exc:  # noqa: BLE001
        return {"backend": "neo4j", "total": 0, "wings": [], "error": str(exc)}
    finally:
        try:
            drv.close()
        except Exception:
            pass


@app.get("/api/memory/stats")
def memory_stats() -> dict:
    from aiforge_core.memory import backend_select as _bsel
    backend = _bsel.memory_backend()
    if backend == "neo4j":
        return _neo4j_stats()
    if backend == "sqlite":
        from aiforge_core.memory import sqlite_memory as _sqlmem
        s = _sqlmem.stats()
        wings = [{"tier": "embedded", "wing": k, "n": v, "embedded": v}
                 for k, v in s.get("by_kind", {}).items()]
        return {"backend": "sqlite", "total": s.get("total", 0), "wings": wings}
    with _db() as c, c.cursor() as cur:
        cur.execute(
            "SELECT tier, wing, COUNT(*) AS n, "
            "COUNT(embedding) AS embedded "
            "FROM memories GROUP BY tier, wing "
            "ORDER BY tier, wing"
        )
        rows = cur.fetchall()
    return {"backend": "postgres", "wings": rows}


@app.get("/api/memory/search")
def memory_search(q: str = Query(..., min_length=2),
                  role: str = Query("sr_developer"),
                  top_k: int = Query(12, le=50)) -> list[dict]:
    from aiforge_core.memory import backend_select as _bsel
    backend = _bsel.memory_backend()
    if backend == "sqlite":
        from aiforge_core.memory import sqlite_memory as _sqlmem
        return [
            {
                "tier": "embedded", "wing": h.get("kind"),
                "source": h.get("source"),
                "text": (h.get("text") or "")[:800], "score": h.get("score"),
                "metadata": {"ticket": h.get("ticket"), "repo": h.get("repo")},
            }
            for h in _sqlmem.recall(q, limit=top_k)
        ]
    if backend == "neo4j":
        # Use the unified recall (afm_bundle + graph hops); map to UI rows.
        from aiforge_core.memory import unified_query as _uq
        res = _uq.query(q, role=role, limit=top_k)
        return [
            {
                "tier": "graph", "wing": h.get("kind") or h.get("source"),
                "source": h.get("source"),
                "text": (h.get("text") or "")[:800],
                "score": h.get("score"),
                "metadata": {k: v for k, v in h.items()
                             if k not in ("text", "score", "source")},
            }
            for h in res.get("hits", [])
        ]
    from aiforge_core.memory.store import Memory
    m = Memory()
    hits = m.search(q, role=role, top_k=top_k)
    return [
        {
            "tier": h.tier, "wing": h.wing, "source": h.source,
            "text": h.text[:800], "score": h.score,
            "metadata": h.metadata,
        }
        for h in hits
    ]


# ───────────────────── Memory sources (ingestion) ──────────────────────
# Register code repos / docs folders / URLs / files and index them into
# the active memory backend. See aiforge_core.runtime.memory_sources +
# memory_ingest.


class _MemSourceBody(BaseModel):
    kind: str = Field(..., description="repo | docs | url | file")
    location: str = Field(..., min_length=1, description="path or URL")
    name: str | None = Field(None)


@app.get("/api/memory/sources")
def memory_sources_list() -> list[dict]:
    from aiforge_core.runtime import memory_sources as _ms
    return _ms.list_sources()


@app.post("/api/memory/sources", status_code=201)
def memory_sources_create(body: _MemSourceBody) -> dict:
    from aiforge_core.runtime import memory_sources as _ms
    try:
        return _ms.create(body.kind, body.location, body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/memory/sources/upload", status_code=201)
async def memory_sources_upload(file: UploadFile = File(...),
                                name: str | None = Form(None)) -> dict:
    """Upload a single file to ingest. Saved under the config dir, then
    registered as a ``file`` source (index it with the /index endpoint)."""
    from aiforge_core.runtime import memory_sources as _ms
    dest_dir = os.path.join(
        os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")),
        "memory-files")
    os.makedirs(dest_dir, exist_ok=True)
    safe = os.path.basename(file.filename or "upload.txt")
    dest = os.path.join(dest_dir, safe)
    with open(dest, "wb") as fh:
        fh.write(await file.read())
    return _ms.create("file", dest, name or safe)


@app.delete("/api/memory/sources/{source_id}", status_code=204)
def memory_sources_delete(source_id: int) -> None:
    from aiforge_core.runtime import memory_sources as _ms
    if not _ms.delete(source_id):
        raise HTTPException(404, f"source {source_id} not found")


@app.post("/api/memory/sources/{source_id}/index")
def memory_sources_index(source_id: int) -> dict:
    """Kick off background indexing of a source into memory."""
    import threading

    from aiforge_core.runtime import memory_sources as _ms
    from aiforge_core.runtime.memory_ingest import run_index
    src = _ms.get(source_id)
    if not src:
        raise HTTPException(404, f"source {source_id} not found")
    _ms.set_status(source_id, "indexing", error=None)
    threading.Thread(target=run_index, args=(source_id,), daemon=True).start()
    return {**src, "status": "indexing"}


# ─────────────────────────── Logs SSE ───────────────────────────────────
# Recognised log files (newest naming first). When no orchestrator-<role>
# exists, fall back to the ADK-prefixed file (current convention) and
# finally the master adk_runner stream so the UI never tails an empty
# legacy file.
def _resolve_role_log(role: str) -> str:
    candidates = [
        os.path.join(LOG_DIR, f"orchestrator-adk.{role}.ndjson"),
        os.path.join(LOG_DIR, f"orchestrator-{role}.ndjson"),
        os.path.join(LOG_DIR, "orchestrator-adk_runner.ndjson"),
    ]
    for p in candidates:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return candidates[0]   # let the tailer wait for the primary to appear


_EXTRA_LOG_ROLES = {"intent", "publish", "integration", "adk_runner"}


@app.get("/api/logs/{role}/stream")
def stream_role_log(role: str):
    if role not in ROLES and role not in _EXTRA_LOG_ROLES:
        raise HTTPException(404, f"unknown role {role!r}")
    path = _resolve_role_log(role)

    async def gen():
        last_size = 0
        if os.path.exists(path):
            last_size = os.path.getsize(path)
        try:
            while True:
                await asyncio.sleep(1.5)
                if not os.path.exists(path):
                    continue
                sz = os.path.getsize(path)
                if sz <= last_size:
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    f.seek(last_size)
                    chunk = f.read()
                last_size = sz
                for line in chunk.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    yield f"data: {line}\n\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(gen(), media_type="text/event-stream")


# ─────────────────────────── Ticket trace SSE ───────────────────────────
#
# Live tail of the graph-runner master log, filtered by ticket identifier.
# The UI /trace/:id view subscribes and renders Step/Action/Observation as
# it arrives so ops can watch a run in progress and decide whether to
# intervene (cancel ticket, swap model, add hint).


@app.get("/api/trace/{identifier}/stream")
def stream_ticket_trace(identifier: str):
    """Tail graph-runner logs on the orchestrator host + stream lines for
    this ticket. Merges the smolagents stdout stream (``graph-runner.log``)
    with the structured NDJSON stream (``graph-runner.err``) so the client
    sees Step/Action/Observation AND ``llm.call`` / agent ndjson events
    (including raw prompt + completion) interleaved in time order.
    """
    host = os.environ.get("AIFORGE_GRAPH_RUNNER_HOST", "").strip()
    log = os.environ.get(
        "AIFORGE_GRAPH_RUNNER_LOG",
        os.path.expanduser("~/.aiforge/logs/graph-runner.log"),
    )
    err = os.environ.get(
        "AIFORGE_GRAPH_RUNNER_ERR",
        os.path.expanduser("~/.aiforge/logs/graph-runner.err"),
    )

    async def gen():
        # One tail per file; interleave via a queue so either stream
        # can deliver a line as soon as it arrives. Run tail locally unless
        # AIFORGE_GRAPH_RUNNER_HOST is set — the api now runs on the same
        # host as the graph-runner, so ssh-to-self was the previous bug.
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def pump(path: str) -> None:
            if host:
                proc = await asyncio.create_subprocess_exec(
                    "ssh", "-o", "ConnectTimeout=5", host,
                    f"tail -Fn500 {path}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    "tail", "-Fn500", path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        await asyncio.sleep(0.3)
                        continue
                    await queue.put(line.decode("utf-8", "replace").rstrip("\n"))
            finally:
                try: proc.kill()
                except Exception: pass
                await queue.put(None)

        tasks = [
            asyncio.create_task(pump(log)),
            asyncio.create_task(pump(err)),
        ]
        in_ctx = False
        try:
            while True:
                raw = await queue.get()
                if raw is None:
                    break

                # Scope management via structured NDJSON events. Accept
                # both legacy (graph_runner.*) and current (adk_runner.*)
                # event names so older + newer runs both stream cleanly.
                _START_MARKERS = (
                    '"event": "graph_runner.start"',
                    '"event":"graph_runner.start"',
                    '"event": "adk_runner.start"',
                    '"event":"adk_runner.start"',
                )
                _DONE_MARKERS = (
                    '"event": "graph_runner.done"',
                    '"event":"graph_runner.done"',
                    '"event": "adk_runner.done"',
                    '"event":"adk_runner.done"',
                )
                if any(m in raw for m in _START_MARKERS):
                    in_ctx = (f'"{identifier}"' in raw)
                elif any(m in raw for m in _DONE_MARKERS) and \
                     f'"{identifier}"' in raw:
                    yield f"data: {json.dumps({'line': raw})}\n\n"
                    in_ctx = False
                    continue

                if in_ctx:
                    yield f"data: {json.dumps({'line': raw})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()

    return StreamingResponse(gen(), media_type="text/event-stream")


_TERMINAL_TICKET = {"done", "qa", "qa_failed", "cancelled"}


@app.get("/api/tickets/{identifier}/events/stream")
def stream_ticket_events(identifier: str) -> StreamingResponse:
    """Live stage updates for a ticket, sourced from ``ticket_events`` in
    the DB (shared across the api + runner containers — unlike the
    log-tail trace). Emits every event for the ticket, then polls for new
    ones; emits the clarification + status when the run pauses awaiting
    the user; closes on a terminal status. Chat Pipeline mode streams
    this."""
    import time as _t

    def _gen():
        t0 = tickets_mod.get(identifier)
        if t0 is None:
            yield f"data: {json.dumps({'kind': 'error', 'body': 'ticket not found'})}\n\n"
            return
        tid = t0.id
        seen: set = set()
        for _ in range(1200):   # ~40 min at 2s
            t = tickets_mod.get(identifier)
            if t is None:
                break
            for e in tickets_mod.comments(tid, 1000):
                eid = e.get("id")
                if eid in seen:
                    continue
                seen.add(eid)
                created = e.get("created_at")
                yield "data: " + json.dumps({
                    "kind": e.get("kind"), "agent_role": e.get("agent_role"),
                    "body": e.get("body") or "",
                    "metadata": e.get("metadata") or {},
                    "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
                }) + "\n\n"
            meta = t.metadata or {}
            awaiting = bool(meta.get("awaiting_input"))
            yield "data: " + json.dumps({
                "kind": "status", "status": t.status, "awaiting_input": awaiting,
                "clarify_questions": meta.get("clarify_questions") or [],
            }) + "\n\n"
            if awaiting:
                break
            if t.status in _TERMINAL_TICKET:
                yield f"data: {json.dumps({'kind': 'done', 'status': t.status})}\n\n"
                break
            if t.status == "blocked":
                yield f"data: {json.dumps({'kind': 'done', 'status': 'blocked'})}\n\n"
                break
            _t.sleep(2)

    return StreamingResponse(_gen(), media_type="text/event-stream")


# ─────────────────────────── LLM call trace ─────────────────────────────
#
# Per-ticket stream of just the ``llm.call`` NDJSON events — full chat
# messages sent to the model + full response content + token usage +
# wall time. Use this when you want to see exactly what each Planner /
# Doer tick said to the LLM and what came back, without the smolagents
# stdout noise.


@app.get("/api/llm-trace/{identifier}/stream")
def stream_llm_trace(identifier: str):
    err = os.environ.get(
        "AIFORGE_GRAPH_RUNNER_ERR",
        os.path.expanduser("~/.aiforge/logs/graph-runner.err"),
    )
    host = os.environ.get("AIFORGE_GRAPH_RUNNER_HOST", "").strip()

    async def gen():
        if host:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "ConnectTimeout=5", host,
                f"tail -Fn2000 {err}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                "tail", "-Fn2000", err,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        needle_event = '"event": "llm.call"'
        needle_event_compact = '"event":"llm.call"'
        needle_ticket = f'"ticket": "{identifier}"'
        needle_ticket_compact = f'"ticket":"{identifier}"'
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    await asyncio.sleep(0.3)
                    continue
                raw = line.decode("utf-8", "replace").rstrip("\n")
                if (needle_event in raw or needle_event_compact in raw) and \
                   (needle_ticket in raw or needle_ticket_compact in raw):
                    yield f"data: {raw}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try: proc.kill()
            except Exception: pass

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/llm-trace/{identifier}")
def list_llm_trace(identifier: str, limit: int = 50):
    """Non-streaming: return the last N ``llm.call`` events for this ticket
    as a JSON list. Easier to inspect in a browser / curl | jq."""
    err = os.environ.get(
        "AIFORGE_GRAPH_RUNNER_ERR",
        os.path.expanduser("~/.aiforge/logs/graph-runner.err"),
    )
    needle_event = '"event": "llm.call"'
    needle_event_compact = '"event":"llm.call"'
    needle_ticket = f'"ticket": "{identifier}"'
    needle_ticket_compact = f'"ticket":"{identifier}"'
    events: list[dict] = []
    try:
        with open(err, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if (needle_event in line or needle_event_compact in line) and \
                   (needle_ticket in line or needle_ticket_compact in line):
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        continue
    except FileNotFoundError:
        return {"events": [], "count": 0, "error": f"{err} not found"}
    events = events[-limit:]
    return {"events": events, "count": len(events)}


# ─────────────────────────── Agent model config ─────────────────────────

from aiforge_core.config import agent_config as _acfg


@app.get("/api/config/agents")
def config_agents_list() -> dict:
    """Per-archetype provider + model map. UI Settings calls this.

    Surfaces the 6 v5 archetype roles
    (architect/planner/verifier/doer/feedback/learner) — the live ADK
    SequentialAgent + external Architect.
    """
    full = _acfg.load_all()
    visible = {r: full[r] for r in _acfg._ARCHETYPES if r in full}
    return {
        "roles": visible,
        "archetype_order": list(_acfg._ARCHETYPES),
        "providers": {
            p["id"]: {"label": p["label"],
                      "default_model": p["default_model"]}
            for p in _acfg.list_providers()
        },
    }


class _AgentConfigBody(BaseModel):
    provider: str = Field(..., description="One of agent_config.PROVIDERS keys")
    model: str = Field(..., description="Model identifier for the provider")


@app.put("/api/config/agents/{role}")
def config_agents_set(role: str, body: _AgentConfigBody) -> dict:
    try:
        cfg = _acfg.set_role(role, body.provider, body.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"role": role, **cfg}


# ─────────────────────── Agent model config v2 ─────────────────────────
# v2 surface for the new Settings UI. Adds per-role base_url, returns the
# full model catalog inline with each provider, and exposes only the 6
# v5 archetype roles. v1 (above) kept untouched so the current UI build
# keeps working until it migrates.


class _AgentConfigV2Body(BaseModel):
    provider: str = Field(..., description="One of agent_config.PROVIDERS keys")
    model: str = Field(..., min_length=1,
                       description="Model identifier for the provider")
    base_url: str | None = Field(
        None, description="Optional override; null = provider default")
    api_key: str | None = Field(
        None, description="Optional API key (openai_compatible cloud-with-key); "
                          "blank = no token")


class _ProviderTestBody(BaseModel):
    base_url: str = Field(..., description="OpenAI-compatible base URL to probe")
    api_key: str | None = Field(None, description="Optional bearer key")


@app.get("/api/agents/v2/config")
def agents_v2_config() -> dict:
    """Return ``{role: {provider, model, base_url|null}}`` for the 6
    v5 archetypes (architect/planner/verifier/doer/feedback/learner)."""
    full = _acfg.load_all()
    out: dict[str, dict[str, Any]] = {}
    for role in _acfg.archetypes():
        row = full.get(role) or {}
        out[role] = {
            "provider": row.get("provider"),
            "model": row.get("model"),
            "base_url": row.get("base_url"),
            # Never echo the secret — just whether one is stored.
            "api_key_set": bool(row.get("api_key")),
        }
    return out


@app.post("/api/providers/test")
def providers_test(body: _ProviderTestBody) -> dict:
    """Test-connection for the home page. Probes ``{base_url}/v1/models``
    and returns ``{ok, models[]}`` (or ``{ok:false, error}``)."""
    from aiforge_core.llm.providers.openai_compatible import probe
    return probe(body.base_url, body.api_key)


@app.get("/api/agents/v2/providers")
def agents_v2_providers() -> list[dict]:
    """Catalog payload for the Settings UI: each provider with its
    available models inline. Includes dynamic discovery for local
    (LM Studio /v1/models) and ollama_cloud (5-min cached)."""
    out: list[dict[str, Any]] = []
    for prov in _acfg.list_providers():
        try:
            models = _acfg.list_models(prov["id"])
        except Exception:
            models = []
        out.append({**prov, "models": models})
    return out


@app.put("/api/agents/v2/{role}/config")
def agents_v2_set(role: str, body: _AgentConfigV2Body) -> dict:
    if role not in _acfg.archetypes():
        raise HTTPException(404, f"unknown archetype: {role}")
    if body.provider not in _acfg.PROVIDERS:
        raise HTTPException(400, f"unknown provider: {body.provider}")
    if not body.model or not body.model.strip():
        raise HTTPException(400, "model cannot be empty")
    base_url = body.base_url.strip() if body.base_url else None
    api_key = body.api_key.strip() if body.api_key else None
    try:
        cfg = _acfg.set_role(role, body.provider, body.model,
                             base_url=base_url or None,
                             api_key=api_key or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "role": role,
        "provider": cfg.get("provider"),
        "model": cfg.get("model"),
        "base_url": cfg.get("base_url"),
        "api_key_set": bool(cfg.get("api_key")),
    }


@app.get("/api/agents/v2/profiles")
def agents_v2_profiles_list() -> dict:
    """Bundled profile presets — apply one to assign all 9 archetypes
    to the same provider/model in one call."""
    return {
        "profiles": [
            {"name": name, **spec}
            for name, spec in _acfg.PROFILES.items()
        ]
    }


@app.put("/api/agents/v2/profile/{name}")
def agents_v2_profile_apply(name: str) -> dict:
    """Bulk-apply a profile preset to every archetype.

    Returns the resulting per-role map. After applying, individual
    archetypes can still be flipped via PUT /api/agents/v2/{role}/config.
    """
    try:
        out = _acfg.apply_profile(name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"profile": name, "roles": out}


# ─────────────────────────── Chat ask (LLM synthesis) ───────────────────
#
# /api/chat/ask is the "smart" chat endpoint: gather from all memory
# tiers + targeted MCP calls + have the LLM synthesize an answer,
# instead of dumping raw hits to the UI. The client sees one natural-
# language answer plus the tools/hits that sourced it.


_CHAT_SYSTEM = """You are the AIForge chat agent. The operator asks
questions about our OneShell codebase / past tickets / decisions. You
answer ONLY from the supplied ``## Context`` block — do NOT invent
file paths, symbols, versions, or commit shas the context doesn't
mention.

Output shape:
- 1-2 line direct answer up top.
- Then a short bullet list of the specific context rows you used
  (cite by [tier] and wing or ticket identifier).
- If the context is too thin to answer, say so in one line and
  suggest which MCP tool the operator should run (sym_lookup,
  cross_repo_flow, ticket_brief, etc.). No apology, no filler.
"""


class _ChatAskBody(BaseModel):
    query: str = Field(..., description="The operator's free-text question")
    top_k: int = Field(12, description="Memory hits per role")
    role: str = Field("planner", description="Retrieval policy role")


_TICKET_RE = re.compile(r"\b(ONE-\d+)\b", re.I)
_CLASS_RE = re.compile(r"\b([A-Z][A-Za-z0-9]{3,})\b")
_REPO_RE = re.compile(r"\b(Pos[A-Z][A-Za-z]+|oneshell-[a-z-]+|MongoDbService|"
                      r"GatewayService|BusinessService|TallyConnector|"
                      r"EmailService|NotificationService|Gst[A-Z][A-Za-z]*|"
                      r"VendorIntegrationService|WhatsappApiService|"
                      r"Scheduler|QuartzScheduler|StoreIntelligence)\b")


def _call_mcp_sync(tool: str, args: dict, timeout: int = 15) -> dict | None:
    """Synchronous one-shot MCP invocation from inside a sync handler."""
    if tool not in _MCP_ALLOWED_TOOLS:
        return None
    import subprocess
    cmd = [os.environ.get(
        "AIFORGE_MCP_BIN",
        "/home/mani/AIForgeCrew/.venv/bin/aiforge-graph-mcp",
    )]
    payload = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "aiforge-ui", "version": "0.1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": tool, "arguments": args}},
    ]) + "\n"
    try:
        proc = subprocess.run(
            cmd, input=payload.encode(), capture_output=True,
            timeout=timeout, check=False,
        )
    except Exception:
        return None
    for line in (proc.stdout or b"").splitlines():
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if isinstance(msg, dict) and msg.get("id") == 2:
            if "error" in msg:
                return None
            return msg.get("result") or {}
    return None


_NORMALIZE_SYSTEM = """You are a query normalizer. The user will send one
short question that may contain typos, bad grammar, or missing articles.
Rewrite it as ONE clean English line that preserves intent, expands
obvious acronyms (pos → pos client backend, wg → wireguard), and fixes
typos. Do NOT answer the question. Do NOT add anything beyond the
rewritten query. Max 200 chars."""


def _normalize_query(query: str) -> str:
    """Tiny LLM pass that cleans typos + grammar so retrieval (BM25 and
    vector) actually hits. Falls back to the raw query on any failure.

    Skipped for queries already clean-ish (length < 12 chars, OR only
    one word) to avoid burning a call on trivial inputs.
    """
    q = query.strip()
    if len(q) < 12 or " " not in q:
        return q
    from aiforge_core.llm import complete as _complete
    try:
        result = _complete(
            "chat",
            [
                {"role": "system", "content": _NORMALIZE_SYSTEM},
                {"role": "user", "content": q[:600]},
            ],
            max_tokens=128, temperature=0.0,
            timeout_s=30,
        )
        if not result:
            return q
        # Strip stray quoting / leading labels.
        result = result.strip().strip('"\' ')
        for prefix in ("normalized:", "query:", "rewritten:"):
            if result.lower().startswith(prefix):
                result = result[len(prefix):].strip()
        return result[:300] or q
    except Exception:
        return q

# ─────────────────────── Chat (thin LLM proxy) ─────────────────────
#
# The legacy GenericAgent / smolagents chat orchestrator was retired.
# /api/chat/ask now calls the unified LLM router directly. Memory-
# grounded chat moved to the agent pipeline (POST /api/tickets) — the
# Understander does the same job there with full context capture.

@app.post("/api/chat/ask")
def chat_ask(body: _ChatAskBody) -> dict:
    """Thin LLM proxy. No memory orchestration, no MCP tools — those
    live in the ticket pipeline now. Use POST /api/tickets for the
    full-featured agent flow."""
    from aiforge_core.orchestrator import llm_client
    answer = llm_client.call_text(
        role="doer",
        system="You are AIForgeCrew's chat assistant. Be concise.",
        user=body.query.strip() or "Hello",
        temperature=0.2,
        max_tokens=2048,
    )
    return {
        "answer": answer or "(empty response)",
        "trace": [],
        "hits": [],
    }


@app.post("/api/chat/retain", status_code=201)
def chat_retain(body: _ChatRetainBody) -> dict:
    """Retention path was tied to the GA agent's auto-suggest. Now a
    no-op stub — explicit memory writes go through the new agent
    pipeline's Learner stage."""
    return {"id": None, "retained": False, "reason": "deprecated"}


class _ChatMessage(BaseModel):
    role: str = Field("user", description="'user' or 'assistant'")
    content: str = Field("", description="message text")


class _ChatAgentBody(BaseModel):
    messages: list[_ChatMessage] = Field(..., description="conversation so far")
    cwd: str | None = Field(None, description="working directory; default workspace")
    role: str = Field("doer", description="archetype whose provider config drives the LLM")


def _default_cwd() -> str:
    return (
        os.environ.get("AIFORGE_WORKSPACE_DIR")
        or os.environ.get("AIFORGE_REPO_ROOT")
        or os.getcwd()
    )


@app.post("/api/chat/agent")
def chat_agent(body: _ChatAgentBody) -> StreamingResponse:
    """Conversational full-filesystem coding agent (SSE).

    Streams ReAct steps — thoughts, tool calls + results, and the final
    message — as ``data: {json}\\n\\n`` events. Drives the provider
    configured for ``role`` on the home page. NOT the ticket pipeline.
    """
    from aiforge_core.runtime.chat_agent import run_chat_agent
    cwd = body.cwd or _default_cwd()
    msgs = [{"role": m.role, "content": m.content} for m in body.messages]

    def _gen():
        try:
            for ev in run_chat_agent(msgs, cwd=cwd, role=body.role):
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'text': str(exc)})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")


# ───────────────────── Persistent chat sessions ────────────────────────
# Claude-style multi-conversation chat: server-stored sessions the user
# can resume/rename/delete. Each session threads full history into the
# agent so a topic continues across turns. Storage: aiforge_core.runtime.
# chat_store (SQLite, survives reloads/redeploys).


class _NewSessionBody(BaseModel):
    title: str | None = Field(None)
    cwd: str | None = Field(None)
    role: str = Field("chat", description="model slot driving chat (default: chat)")


def _served_model_ids(provider: str) -> set:
    """IDs the provider is currently serving (active/loaded). For local /
    ollama_cloud this hits /v1/models; empty set when undiscoverable."""
    try:
        return {m.get("id") for m in (_acfg.list_models(provider) or [])
                if m.get("id")}
    except Exception:
        return set()


@app.get("/api/chat/models")
def chat_models() -> dict:
    """Models for the dedicated 'chat' slot. Lists only the provider's
    currently-served (active) models, flags whether the saved selection
    is still active so the UI can warn / re-pick."""
    row = _acfg.get("chat") if "chat" in _acfg.archetypes() else {}
    provider = row.get("provider") or "local"
    served = _served_model_ids(provider)
    current = row.get("model")
    return {
        "provider": provider,
        "current": current,
        "current_active": (current in served) if served else True,
        "models": [{"id": mid, "label": mid.split("/")[-1], "active": True}
                   for mid in sorted(served)],
    }


class _ChatModelBody(BaseModel):
    model: str = Field(..., min_length=1)
    provider: str | None = Field(None)


@app.put("/api/chat/model")
def chat_model_set(body: _ChatModelBody) -> dict:
    """Persist the chat slot's model + report whether it's active (served
    right now). Rejected only on bad input — an inactive model is saved
    but flagged so the UI can warn."""
    cur = _acfg.get("chat") if "chat" in _acfg.archetypes() else {}
    provider = body.provider or cur.get("provider") or "local"
    try:
        cfg = _acfg.set_role("chat", provider, body.model,
                             base_url=cur.get("base_url"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    served = _served_model_ids(provider)
    return {"provider": cfg.get("provider"), "model": cfg.get("model"),
            "active": (cfg.get("model") in served) if served else True}


class _RenameBody(BaseModel):
    title: str = Field(..., min_length=1)


class _SessionMsgBody(BaseModel):
    content: str = Field(..., min_length=1)
    role: str | None = Field(None, description="override the session's model (archetype)")
    mode: str = Field("simple", description="'simple' (single agent) | 'team' (full ADK flow)")


def _chat_workspace_root() -> str:
    return os.environ.get(
        "AIFORGE_CHAT_WORKSPACE_ROOT",
        os.path.join(os.path.expanduser(
            os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")), "chat-workspaces"))


@app.post("/api/chat/sessions", status_code=201)
def chat_session_create(body: _NewSessionBody) -> dict:
    from aiforge_core.runtime import chat_store
    s = chat_store.create_session(body.title or "New chat",
                                  body.cwd or _default_cwd(),
                                  role=body.role or "chat")
    # Isolation: when the caller didn't pin a cwd, give the session its
    # own workspace dir so it can build/clean/run without touching other
    # sessions or the host. Persisted under app_state on the compose deploy.
    if not body.cwd:
        ws = os.path.join(_chat_workspace_root(), f"session-{s['id']}")
        try:
            os.makedirs(ws, exist_ok=True)
            s = chat_store.set_session_cwd(s["id"], ws) or s
        except OSError:
            pass
    return s


@app.get("/api/chat/sessions")
def chat_session_list() -> list[dict]:
    from aiforge_core.runtime import chat_store
    return chat_store.list_sessions()


@app.get("/api/chat/sessions/{session_id}")
def chat_session_get(session_id: int) -> dict:
    from aiforge_core.runtime import chat_store
    s = chat_store.get_session(session_id)
    if not s:
        raise HTTPException(404, f"session {session_id} not found")
    return {"session": s, "messages": chat_store.get_messages(session_id)}


@app.patch("/api/chat/sessions/{session_id}")
def chat_session_rename(session_id: int, body: _RenameBody) -> dict:
    from aiforge_core.runtime import chat_store
    s = chat_store.rename_session(session_id, body.title)
    if not s:
        raise HTTPException(404, f"session {session_id} not found")
    return s


@app.delete("/api/chat/sessions/{session_id}", status_code=204)
def chat_session_delete(session_id: int) -> None:
    from aiforge_core.runtime import chat_store
    if not chat_store.delete_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")


@app.post("/api/chat/sessions/{session_id}/message")
def chat_session_message(session_id: int, body: _SessionMsgBody) -> StreamingResponse:
    """Append a user message, run the full-FS coding agent over the whole
    session history (Claude-CLI-style: many tool steps, builds repos),
    stream every step as SSE, and persist the assistant reply + steps.
    Auto-titles a fresh session. The model is the session's role
    (model picker)."""
    from aiforge_core.runtime import chat_store
    from aiforge_core.runtime.chat_agent import run_chat_agent
    from aiforge_core.runtime.chat_pipeline import stream_chat_pipeline

    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")

    role = body.role or session.get("role") or "chat"
    if body.role and body.role != session.get("role"):
        chat_store.set_session_role(session_id, body.role)

    chat_store.add_message(session_id, "user", body.content)
    if (session.get("title") or "New chat") == "New chat":
        chat_store.rename_session(session_id, body.content.strip()[:60])

    history = [
        {"role": m["role"], "content": m["content"]}
        for m in chat_store.get_messages(session_id)
        if m["role"] in ("user", "assistant") and m["content"]
    ]
    cwd = session.get("cwd") or _default_cwd()
    team = body.mode == "team"
    prompt = body.content.strip()

    def _events():
        # Team mode → full ADK agent flow (planner→…→learner) for complex
        # builds. Simple mode → single conversational agent for quick work.
        if team:
            return stream_chat_pipeline(prompt, cwd=cwd)
        return run_chat_agent(history, cwd=cwd, role=role)

    def _gen():
        steps: list[dict] = []
        final_text = ""
        try:
            for ev in _events():
                if ev.get("type") == "message":
                    final_text = ev.get("text", "")
                elif ev.get("type") in ("thought", "tool", "error"):
                    steps.append(ev)
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'text': str(exc)})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        finally:
            chat_store.add_message(session_id, "assistant", final_text, steps)

    return StreamingResponse(_gen(), media_type="text/event-stream")


class _SessionTicketBody(BaseModel):
    content: str = Field(..., min_length=1)
    project: str | None = Field(None, description="target repo; defaults to session cwd name")


@app.post("/api/chat/sessions/{session_id}/ticket", status_code=201)
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
    if (session.get("title") or "New chat") == "New chat":
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


class _TicketAnswerBody(BaseModel):
    content: str = Field(..., min_length=1)


@app.post("/api/tickets/{identifier}/answer")
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


_MCP_ALLOWED_TOOLS = {
    "sym_lookup", "list_repos", "list_services", "list_endpoints",
    "list_integrations", "graph_neighborhood", "caller_chain",
    "callee_chain", "read_source", "impact", "cross_repo_flow",
    "data_lineage", "build_plan", "test_plan", "kube_status",
    "kube_describe", "kube_image_tag", "kube_config", "find_doc",
    "related_memories", "ticket_fetch", "ticket_brief",
}


class _McpCallBody(BaseModel):
    tool: str = Field(..., description="Tool name from graph_rag MCP allowlist")
    args: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/mcp/tool")
async def mcp_tool_call(body: _McpCallBody) -> dict:
    if body.tool not in _MCP_ALLOWED_TOOLS:
        raise HTTPException(400, f"tool '{body.tool}' not in allowlist")
    cmd = [
        os.environ.get("AIFORGE_MCP_BIN",
                       "/home/mani/AIForgeCrew/.venv/bin/aiforge-graph-mcp"),
    ]
    env = {
        **os.environ,
        "AIFORGE_NEO4J_URI": os.environ.get(
            "AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687"),
        "AIFORGE_NEO4J_USER": os.environ.get("AIFORGE_NEO4J_USER", "neo4j"),
        "AIFORGE_NEO4J_PASSWORD": os.environ.get(
            "AIFORGE_NEO4J_PASSWORD", "password"),
        # graph_rag/cypher_lib reads NEO4J_URI / NEO4J_USER / NEO4J_PASS
        # (no AIFORGE_ prefix); mirror so the subprocess can connect.
        "NEO4J_URI": os.environ.get(
            "AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687"),
        "NEO4J_USER": os.environ.get("AIFORGE_NEO4J_USER", "neo4j"),
        "NEO4J_PASS": os.environ.get(
            "AIFORGE_NEO4J_PASSWORD", "password"),
        # Embed sidecar — graph_mcp defaults to :1235/v1 (planner LLM
        # port) which 404s. Force the real sidecar URL for this run.
        "EMBED_URL": os.environ.get(
            "EMBED_URL", "http://127.0.0.1:8764"),
        "AIFORGE_DSN": os.environ.get(
            "AIFORGE_DSN",
            "postgresql://aiforge:aiforgepass@127.0.0.1:5432/aiforge"),
    }

    # JSON-RPC dance: initialize → tools/call → shutdown.
    init_req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {}},
                           "clientInfo": {"name": "aiforge-ui",
                                          "version": "0.1"}}}
    init_notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    tool_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": body.tool, "arguments": body.args}}
    payload = (
        json.dumps(init_req) + "\n" +
        json.dumps(init_notify) + "\n" +
        json.dumps(tool_req) + "\n"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(
            proc.communicate(payload.encode()), timeout=30,
        )
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        raise HTTPException(504, "MCP server timed out")
    except FileNotFoundError:
        raise HTTPException(503, f"MCP binary not found: {cmd[0]}")

    # Scan stdout line by line for the JSON-RPC response to id=2.
    result: dict | None = None
    for line in out.splitlines():
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if isinstance(msg, dict) and msg.get("id") == 2:
            result = msg
            break
    if result is None:
        raise HTTPException(
            500, f"MCP call produced no response. stderr={err[:400]!r}",
        )
    if "error" in result:
        raise HTTPException(400, f"MCP error: {result['error']}")
    return {"tool": body.tool, "result": result.get("result")}


# ─────────────────────────── Workflow topology (DAG view) ──────────────
@app.get("/api/runtime/perf")
def runtime_perf(reset: bool = False) -> dict:
    """Per-step perf snapshot. Empty under new aiforge_agents pipeline
    until the orchestrator's audit log is wired into a perf aggregator.
    The legacy GA hooks module was removed."""
    return {"rows": [], "reset": bool(reset)}


def _static_topology() -> dict:
    """Static v6 pipeline DAG — fallback when no live topology module is
    present, so the Workflow view renders instead of erroring."""
    stages = ["triage", "planner", "verifier", "researcher",
              "doer", "refiner", "feedback", "learner"]
    nodes = [{"id": s, "label": s, "type": "agent", "tools": [],
              "status": "idle", "last_event_at": None} for s in stages]
    edges = [{"from": stages[i], "to": stages[i + 1], "label": ""}
             for i in range(len(stages) - 1)]
    return {"nodes": nodes, "edges": edges, "ticket": None, "static": True}


def _topology_snapshot(ticket: str | None) -> dict:
    try:
        from aiforge_core.runtime import workflow_topology as _wt
        return _wt.snapshot(ticket)
    except Exception:
        return _static_topology()


@app.get("/api/workflow/topology")
def workflow_topology(ticket: str | None = None) -> dict:
    """DAG snapshot for the UI graph view. Optional ?ticket=X overlays
    per-node status + last_event_at. Falls back to a static pipeline DAG
    when no live topology module is available."""
    return _topology_snapshot(ticket)


@app.get("/api/workflow/stream")
def workflow_stream(ticket: str | None = None,
                    interval: int = 3) -> StreamingResponse:
    """SSE topology refresh. Emits one snapshot every ``interval``
    seconds (clamped 1..30). UI ``EventSource`` consumes for live
    DAG status. Disconnect-safe — generator exits when client closes.
    """
    interval = max(1, min(int(interval or 3), 30))

    def _gen():
        import time as _t
        while True:
            snap = _topology_snapshot(ticket)
            yield f"data: {json.dumps(snap)}\n\n"
            _t.sleep(interval)

    return StreamingResponse(_gen(), media_type="text/event-stream")


# ─────────────────────────── Cost dashboard ────────────────────────────
@app.get("/api/runtime/cost")
def runtime_cost(
    ticket: str | None = None,
    group_by: str | None = None,
    days_back: int = 30,
) -> dict:
    """USD totals.

    Without params: in-memory global + per-ticket map.
    ``?ticket=X`` returns single ticket counters.
    ``?group_by=day|role|model|ticket`` runs SQL rollup over
    ``llm_costs`` for the last ``days_back`` days.
    """
    from aiforge_core.observability import cost as _cost
    if group_by:
        return {"group_by": group_by, "days_back": days_back,
                "rows": _cost.rollup(group_by, days_back=days_back)}
    return _cost.snapshot(ticket)


# ─────────────────────────── Repo standards ────────────────────────────
@app.get("/api/repo/standards")
def repo_standards_get(
    name: str = Query(..., description="Repo name (matches :Repo.name)"),
    worktree: str | None = None,
) -> dict:
    """Resolved per-project standards (commands + conventions)."""
    from aiforge_core.runtime import repo_standards as _rs
    std = _rs.get(name, worktree=worktree)
    return {
        "name": std.name, "lang": std.lang, "stack": std.stack,
        "ports": std.ports, "dockerfile": std.dockerfile,
        "entry_cmd": std.entry_cmd, "build_cmd": std.build_cmd,
        "compile_cmd": std.compile_cmd, "test_cmd": std.test_cmd,
        "lint_cmd": std.lint_cmd, "format_cmd": std.format_cmd,
        "security_scan_cmd": std.security_scan_cmd,
        "conventions": std.conventions,
        "forbidden_patterns": std.forbidden_patterns,
        "env_vars": std.env_vars,
        "acceptance_criteria": std.acceptance_criteria,
        "source": std.source,
    }


class _StandardsBody(BaseModel):
    build_cmd: str | None = None
    compile_cmd: str | None = None
    test_cmd: str | None = None
    lint_cmd: str | None = None
    format_cmd: str | None = None
    security_scan_cmd: str | None = None
    entry_cmd: str | None = None
    conventions: list[str] | None = None
    forbidden_patterns: list[str] | None = None
    env_vars: list[str] | None = None
    acceptance_criteria: list[str] | None = None
    lang: str | None = None
    stack: list[str] | None = None
    ports: list[int] | None = None


@app.put("/api/repo/standards/{name}")
def repo_standards_set(name: str, body: _StandardsBody) -> dict:
    """Persist standards onto the Neo4j ``:Repo`` node."""
    from aiforge_core.runtime import repo_standards as _rs
    std = _rs.upsert(name, **{k: v for k, v in body.model_dump().items()
                              if v is not None})
    return repo_standards_get(name=name)


# ─────────────────────── Ticket file attachments ────────────────────────
# Operator-uploaded files persisted by ``_persist_ticket_attachments``
# under ``{AIFORGE_REPO_ROOT}/.aiforge/ticket-files/{identifier}/``.
# Mount as a static route so the UI can render image thumbnails inline
# and offer download links for non-image files. Names were sanitized at
# upload (``_Path(f.name).name``) so path-traversal is contained to the
# per-ticket subdir.
_TICKET_FILES_ROOT = os.path.join(
    os.path.expanduser(os.environ.get("AIFORGE_REPO_ROOT", "~/aiforge_workspace")),
    ".aiforge", "ticket-files",
)
os.makedirs(_TICKET_FILES_ROOT, exist_ok=True)
app.mount(
    "/files",
    StaticFiles(directory=_TICKET_FILES_ROOT, check_dir=False),
    name="ticket-files",
)

# ─────────────────────────── Static UI ──────────────────────────────────
# If the Vite production build exists, serve it at /ui/ and redirect "/" to it.
_DIST = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "web", "dist"))

if os.path.isdir(_DIST):
    # SPA fallback: any unknown path under /ui/ returns index.html so
    # react-router can handle the route client-side.
    class _SpaStatic(StaticFiles):
        async def get_response(self, path: str, scope):
            try:
                resp = await super().get_response(path, scope)
            except Exception:
                resp = FileResponse(os.path.join(_DIST, "index.html"))
            # index.html / the SPA shell must never be cached, or a deploy
            # leaves users on a stale bundle that references deleted asset
            # hashes ("everything broken" after an update). The hashed
            # assets under /ui/assets/ stay cacheable.
            if path in ("", "/", "index.html") or not path.startswith("assets/"):
                if getattr(resp, "media_type", "") == "text/html" or path in ("", "/", "index.html"):
                    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            return resp

    app.mount("/ui", _SpaStatic(directory=_DIST, html=True), name="ui")

    @app.get("/")
    def _root_redirect():
        # Real 307 redirect to /ui/. Returning index.html directly
        # makes the browser load the bundle at path "/" but the SPA
        # router is mounted at basename="/ui" — first render shows
        # only the static <title> with an empty <div id="root">.
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/ui/", status_code=307)
else:
    @app.get("/")
    def _root_info() -> dict:
        return {
            "service": "aiforge api",
            "hint": "run `cd web && npm run build` to serve the UI at /ui/",
            "routes": [r.path for r in app.routes if hasattr(r, "path")],
        }
