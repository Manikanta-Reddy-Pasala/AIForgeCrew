"""FastAPI backend for the dashboard UI.

Exposes the aiforge Postgres state + live log tails as a small REST + SSE
surface the React/Vite frontend talks to.

Run:
    uvicorn aiforge_core.runtime.api:app --host 127.0.0.1 --port 8799 --reload

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
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import tickets as tickets_mod
from . import config as _cfg
from .config import (
    AIFORGE_DSN, LM_STUDIO_BASE_URL, LOG_DIR, ROLES,
)


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
    from . import otel as _otel
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
    status = {"ok": True, "postgres": False, "lm_studio": False}
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        status["postgres"] = True
    except Exception:
        status["ok"] = False
    try:
        import urllib.request
        with urllib.request.urlopen(
            f"{LM_STUDIO_BASE_URL}/models", timeout=3) as r:
            status["lm_studio"] = r.getcode() == 200
    except Exception:
        pass
    return status


@app.get("/api/agents")
def list_agents() -> list[dict]:
    """Static role catalogue + dynamic last-activity from ticket_events."""
    out = []
    with _db() as c, c.cursor() as cur:
        for name, rc in ROLES.items():
            cur.execute(
                "SELECT MAX(created_at) AS last_activity, "
                "COUNT(*) FILTER (WHERE kind='llm_turn') AS turns "
                "FROM ticket_events WHERE agent_role = %s",
                (name,),
            )
            row = cur.fetchone() or {}
            last = row.get("last_activity")
            cur.execute(
                "SELECT identifier, status FROM tickets "
                "WHERE assignee_role = %s AND status IN "
                "('todo','in_progress','in_review') ORDER BY created_at DESC",
                (name,),
            )
            active = [{"identifier": r["identifier"], "status": r["status"]}
                      for r in cur.fetchall()]
            out.append({
                "role": name,
                "model": rc.model,
                "transport": rc.transport,
                "max_turns": rc.max_turns,
                "tool_allowlist": list(rc.tool_allowlist),
                "last_activity": last.isoformat() if last else None,
                "lifetime_turns": row.get("turns", 0),
                "active_tickets": active,
            })
    return out


# ─────────────────────────── Tickets ────────────────────────────────────
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


class TicketPatch(BaseModel):
    status: str | None = None
    assignee_role: str | None = None
    labels: list[str] | None = None
    body: str | None = None
    max_turns: int | None = None
    metadata: dict | None = None


class CommentCreate(BaseModel):
    body: str
    author: str = "human"


@app.get("/api/tickets")
def list_tickets(role: str | None = Query(None),
                 status: str | None = Query(None),
                 parent: str | None = Query(None),
                 limit: int = Query(100, le=500)) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if role:
        clauses.append("assignee_role = %s"); params.append(role)
    if status:
        statuses = [s.strip() for s in status.split(",")]
        clauses.append("status = ANY(%s)"); params.append(statuses)
    if parent:
        clauses.append("parent_id = (SELECT id FROM tickets WHERE identifier=%s)")
        params.append(parent)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    # Subquery: derive `active_role` = the role of the most-recent agent
    # event on this ticket. Falls back to NULL when no agent has fired
    # yet (so the UI still has assignee_role to show). Distinct from the
    # static `assignee_role` (which is auto-set to "supervisor" by the
    # routing default and never gets overwritten).
    active_role_expr = (
        "(SELECT agent_role FROM ticket_events "
        " WHERE ticket_id=tickets.id AND agent_role IS NOT NULL "
        " ORDER BY created_at DESC LIMIT 1) AS active_role"
    )
    q = (
        "SELECT tickets.*, "
        "(SELECT MIN(created_at) FROM ticket_events "
        " WHERE ticket_id=tickets.id AND kind='status_change' AND body='in_progress'"
        ") AS started_at, "
        f"{active_role_expr} "
        f"FROM tickets{where} ORDER BY id DESC LIMIT %s"
    )
    params.append(limit)
    with _db() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = cur.fetchall()
    return [_ticket_row_out(r) for r in rows]


@app.get("/api/tickets/{identifier}")
def get_ticket(identifier: str) -> dict:
    _started_expr = (
        "(SELECT MIN(created_at) FROM ticket_events "
        " WHERE ticket_id=tickets.id AND kind='status_change' AND body='in_progress'"
        ") AS started_at"
    )
    _active_role_expr = (
        "(SELECT agent_role FROM ticket_events "
        " WHERE ticket_id=tickets.id AND agent_role IS NOT NULL "
        " ORDER BY created_at DESC LIMIT 1) AS active_role"
    )
    with _db() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT tickets.*, {_started_expr}, {_active_role_expr} "
            f"FROM tickets WHERE identifier=%s",
            (identifier,),
        )
        t = cur.fetchone()
        if not t:
            raise HTTPException(404, f"ticket {identifier} not found")
        ticket_id = t["id"]
        cur.execute(
            "SELECT * FROM ticket_events WHERE ticket_id=%s "
            "ORDER BY created_at ASC LIMIT 500",
            (ticket_id,),
        )
        events = [_event_row_out(r) for r in cur.fetchall()]
        cur.execute(
            f"SELECT tickets.*, {_started_expr}, {_active_role_expr} "
            "FROM tickets WHERE parent_id=%s ORDER BY created_at ASC",
            (ticket_id,),
        )
        children = [_ticket_row_out(r) for r in cur.fetchall()]
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
    assignee = _cfg.canonical_role(payload.assignee_role) if payload.assignee_role else None
    t = tickets_mod.create(
        title=payload.title, body=payload.body,
        assignee_role=assignee,
        priority=payload.priority, parent_id=parent_id,
        project=payload.project, labels=payload.labels,
        metadata=md or None,
    )
    if not t.branch:
        t.branch = _derive_branch(t.identifier, t.title)
        try:
            with tickets_mod._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE tickets SET branch=%s WHERE id=%s",
                        (t.branch, t.id),
                    )
                conn.commit()
        except Exception:
            pass
    return _ticket_row_out({
        "id": t.id, "identifier": t.identifier, "title": t.title,
        "body": t.body, "status": t.status, "priority": t.priority,
        "assignee_role": t.assignee_role, "parent_id": t.parent_id,
        "branch": t.branch, "project": t.project, "labels": t.labels,
        "metadata": t.metadata, "created_at": t.created_at,
        "updated_at": t.updated_at, "completed_at": t.completed_at,
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
    """Token totals per role per ticket since runtime start."""
    from aiforge_core.doer.ga_tools import tokens as _tk
    if ticket:
        return _tk.snapshot_for_ticket(ticket)
    return {"all": _tk.snapshot_all()}


@app.get("/api/runtime/rate_limits")
def get_rate_limits() -> dict:
    """Active rate-limit config + bucket state per provider.

    UI uses this to render bucket gauges and the limit-edit form.
    """
    from aiforge_core.llm import list_providers as _list, rl_state as _state
    from aiforge_core.llm import providers as _providers
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
@app.get("/api/memory/stats")
def memory_stats() -> dict:
    with _db() as c, c.cursor() as cur:
        cur.execute(
            "SELECT tier, wing, COUNT(*) AS n, "
            "COUNT(embedding) AS embedded "
            "FROM memories GROUP BY tier, wing "
            "ORDER BY tier, wing"
        )
        rows = cur.fetchall()
    return {"wings": rows}


@app.get("/api/memory/search")
def memory_search(q: str = Query(..., min_length=2),
                  role: str = Query("sr_developer"),
                  top_k: int = Query(12, le=50)) -> list[dict]:
    from .memory import Memory
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


# ─────────────────────────── Logs SSE ───────────────────────────────────
@app.get("/api/logs/{role}/stream")
def stream_role_log(role: str):
    if role not in ROLES:
        raise HTTPException(404, f"unknown role {role!r}")
    path = os.path.join(LOG_DIR, f"orchestrator-{role}.ndjson")

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
        "/Users/manikanta/.aiforge/logs/graph-runner.log",
    )
    err = os.environ.get(
        "AIFORGE_GRAPH_RUNNER_ERR",
        "/Users/manikanta/.aiforge/logs/graph-runner.err",
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

                # Scope management via structured NDJSON events
                if '"event": "graph_runner.start"' in raw or \
                   '"event":"graph_runner.start"' in raw:
                    in_ctx = (f'"{identifier}"' in raw)
                elif ('"event": "graph_runner.done"' in raw or
                      '"event":"graph_runner.done"' in raw) and \
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
        "/Users/manikanta/.aiforge/logs/graph-runner.err",
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
        "/Users/manikanta/.aiforge/logs/graph-runner.err",
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

from . import agent_config as _acfg


@app.get("/api/config/agents")
def config_agents_list() -> dict:
    """Per-agent provider + model map. UI Settings page calls this."""
    return {
        "roles": _acfg.load_all(),
        "providers": {
            k: {"label": v["label"], "default_model": v["default_model"]}
            for k, v in _acfg.PROVIDERS.items()
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


def _collect_chat_context(query: str, role: str, top_k: int) -> dict:
    """Gather memory hits + targeted MCP calls based on what the query mentions."""
    from .memory import Memory
    mem = Memory()
    hits = mem.search(query, role=role, top_k=top_k)

    mcp_results: list[dict] = []

    # If the query mentions a ticket (ONE-xx), pull its brief.
    for m in _TICKET_RE.finditer(query):
        r = _call_mcp_sync("ticket_brief", {"identifier": m.group(1)})
        if r:
            mcp_results.append({"tool": "ticket_brief",
                                "args": {"identifier": m.group(1)},
                                "result": _extract_text(r)})
            break  # one ticket brief is enough

    # If it looks like a class / symbol name (CamelCase word), try sym_lookup.
    syms: set[str] = set()
    for m in _CLASS_RE.finditer(query):
        syms.add(m.group(1))
    for s in list(syms)[:2]:  # cap at 2 to keep context small
        r = _call_mcp_sync("sym_lookup", {"name": s})
        if r:
            mcp_results.append({"tool": "sym_lookup",
                                "args": {"name": s},
                                "result": _extract_text(r)})

    # If a known repo name appears, fetch its brief metadata via list_repos
    # (cheap — one call covers all, filter in LLM).
    if _REPO_RE.search(query) and not mcp_results:
        r = _call_mcp_sync("list_repos", {})
        if r:
            mcp_results.append({"tool": "list_repos",
                                "args": {},
                                "result": _extract_text(r)[:4000]})

    # Always pull related_memories — captures non-obvious overlaps.
    r = _call_mcp_sync("related_memories", {"query": query, "top_k": 6})
    if r:
        mcp_results.append({"tool": "related_memories",
                            "args": {"query": query, "top_k": 6},
                            "result": _extract_text(r)[:2000]})

    return {"hits": hits, "mcp": mcp_results}


def _extract_text(mcp_result: dict) -> str:
    """Pull the ``content[0].text`` field from a standard MCP tool response."""
    try:
        return mcp_result["content"][0]["text"]
    except Exception:
        return json.dumps(mcp_result)[:4000]


def _build_chat_prompt(query: str, ctx: dict) -> str:
    lines = ["## Context\n", "### Memory hits\n"]
    for h in ctx["hits"][:16]:
        # h may be SearchResult dataclass or dict
        tier = getattr(h, "tier", None) or (h.get("tier") if isinstance(h, dict) else "?")
        wing = getattr(h, "wing", None) or (h.get("wing") if isinstance(h, dict) else "?")
        text = getattr(h, "text", None) or (h.get("text") if isinstance(h, dict) else "")
        lines.append(f"- [{tier} {wing}] {text[:260]}")
    if ctx["mcp"]:
        lines.append("\n### MCP tool output\n")
        for m in ctx["mcp"]:
            lines.append(f"- tool={m['tool']} args={m['args']}:")
            for row in m["result"].splitlines()[:40]:
                lines.append("    " + row[:200])
    lines.append(f"\n## Question\n{query}")
    return "\n".join(lines)


def _call_llm_chat(prompt: str) -> str:
    """One-shot LLM call for chat synthesis. Routes via llm.complete()."""
    from aiforge_core.llm import complete as _complete
    return _complete(
        "chat",
        [
            {"role": "system", "content": _CHAT_SYSTEM},
            {"role": "user", "content": prompt[:30_000]},
        ],
        max_tokens=2048, temperature=0.1, timeout_s=120,
    ) or "(empty reply)"


# ─── GA-backed chat agent ───────────────────────────────────────────
#
# Chat now runs through GenericAgent's text-protocol loop (LLMSession +
# ToolClient + agent_runner_loop). Tools are read-only: search_memory
# (Neo4j-backed) + the graph_rag MCP allowlist. After every successful
# answer we auto-retain the Q+A as a T3 chat_qa fact so the next
# similar query benefits from the prior synthesis without needing the
# operator to click "did this work?". See _auto_retain_chat below.

_CHAT_GA_PREAMBLE = """You are the AIForge chat agent. The operator
asked a question about our OneShell codebase / past tickets /
decisions / live ops state. You have read-only access to:
  - search_memory (T1..T4 over Neo4j)
  - graph_rag MCP allowlist (sym_lookup, impact, cross_repo_flow,
    caller_chain, callee_chain, read_source, ticket_brief,
    related_memories, find_doc, list_services, list_repos,
    list_endpoints, graph_neighborhood, data_lineage, build_plan,
    test_plan, kube_status, kube_describe, kube_image_tag,
    kube_config, ticket_fetch, list_integrations)
  - Ops MCPs (mongo / k8s / tekton / tally) exposed as
    ``ops_<server>_<tool>`` for live cluster state.

HARD RULES:
1. PREFER ``unified_memory_query`` first — one call merges 5 sources
   (memory + ticket_brief + related_memories + sym_lookup + find_doc
   + external docs). Only fall back to single-source tools when the
   unified result is thin.
2. After 1-3 tool calls you MUST call ``final_answer`` — do NOT
   keep tool-calling indefinitely. Even if evidence is partial,
   answer with what you have and say what is unknown.
3. Cite file:line when you quote code and [ticket-id] when you
   quote a ticket.
4. If tools return nothing useful, call ``final_answer`` with one
   line saying so — do not invent file paths, symbols, or ticket
   details.

Reply format inside ``final_answer.answer``: 1-2 line direct answer,
then a short bullet list of the concrete evidence you used (tool →
what it returned → conclusion).
"""


_CHAT_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "unified_memory_query",
            "description": (
                "ONE call that fans out to memory + ticket_brief + "
                "related_memories + sym_lookup + find_doc + external "
                "docs and returns a single ranked result list. "
                "PREFER this over multiple narrow tool calls — it is "
                "faster and gives merged ranking across sources."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "ticket": {"type": "string",
                               "description": "Optional ticket id (e.g. ONE-39)"},
                    "role":   {"type": "string",
                               "description": "Retrieval role policy",
                               "default": "sr_developer"},
                    "limit":  {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "Hybrid retrieval over Neo4j-backed T1..T4 memory. "
                "Returns ranked hits with tier + wing + text snippet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "role": {
                        "type": "string",
                        "default": "sr_developer",
                        "description": "Role policy for retrieval tuning",
                    },
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "final_answer",
            "description": "Emit the final answer to the operator. Ends the loop.",
            "parameters": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        },
    },
]
# Minimal schemas for the graph_rag MCP allowlist. We only declare the
# arg names we actually pass through; the MCP server validates the
# rest. Keeping the schema thin saves prompt tokens at chat scale.
_GRAPH_MCP_SCHEMAS: list[dict] = [
    {"name": "sym_lookup", "args": ["name", "kind", "repo"]},
    {"name": "ticket_brief", "args": ["ticket_id"]},
    {"name": "ticket_fetch", "args": ["ticket_id"]},
    {"name": "related_memories", "args": ["query", "top_k"]},
    {"name": "find_doc", "args": ["query", "top_k"]},
    {"name": "read_source", "args": ["path", "start_line", "end_line"]},
    {"name": "caller_chain", "args": ["symbol", "depth"]},
    {"name": "callee_chain", "args": ["symbol", "depth"]},
    {"name": "impact", "args": ["symbol"]},
    {"name": "cross_repo_flow", "args": ["from_symbol", "to_symbol"]},
    {"name": "graph_neighborhood", "args": ["node_id", "depth"]},
    {"name": "data_lineage", "args": ["entity"]},
    {"name": "build_plan", "args": ["repo"]},
    {"name": "test_plan", "args": ["repo"]},
    {"name": "kube_status", "args": ["namespace"]},
    {"name": "kube_describe", "args": ["kind", "name", "namespace"]},
    {"name": "kube_image_tag", "args": ["deployment", "namespace"]},
    {"name": "kube_config", "args": ["namespace"]},
    {"name": "list_repos", "args": []},
    {"name": "list_services", "args": []},
    {"name": "list_endpoints", "args": ["repo"]},
    {"name": "list_integrations", "args": []},
]
for _s in _GRAPH_MCP_SCHEMAS:
    _CHAT_TOOLS_SCHEMA.append({
        "type": "function",
        "function": {
            "name": _s["name"],
            "description": f"graph_rag MCP: {_s['name']}",
            "parameters": {
                "type": "object",
                "properties": {a: {"type": "string"} for a in _s["args"]},
                "required": [],
            },
        },
    })

_GRAPH_MCP_ALLOWED = {s["name"] for s in _GRAPH_MCP_SCHEMAS}


# Ops MCP discovery cache. We probe oneshell-mcp servers (mongo / k8s
# / tekton / tally) once per process; the catalogue itself is cached
# inside mcp_http for 5 minutes. Disable via AIFORGE_CHAT_OPS_MCP=0.
_OPS_TOOLS_SCHEMAS: list[dict] | None = None
_OPS_TOOLS_NAME_MAP: dict[str, tuple[str, str]] = {}


def _ops_tool_schemas() -> tuple[list[dict], dict[str, tuple[str, str]]]:
    """Return ops tool schemas + name→(url, raw_tool) map. Memoised."""
    global _OPS_TOOLS_SCHEMAS, _OPS_TOOLS_NAME_MAP
    if _OPS_TOOLS_SCHEMAS is not None:
        return _OPS_TOOLS_SCHEMAS, _OPS_TOOLS_NAME_MAP
    if os.environ.get("AIFORGE_CHAT_OPS_MCP", "1") != "1":
        _OPS_TOOLS_SCHEMAS = []
        return _OPS_TOOLS_SCHEMAS, _OPS_TOOLS_NAME_MAP
    try:
        from .mcp_http import (
            all_tools_with_origin, render_schema_for_openai,
        )
        discovered = all_tools_with_origin()
        schemas, name_map = render_schema_for_openai(discovered, prefix="ops_")
        _OPS_TOOLS_SCHEMAS = schemas
        _OPS_TOOLS_NAME_MAP = name_map
        print(f"[chat_ga] ops MCPs: {len(schemas)} tools across "
              f"{len({u for u,_ in name_map.values()})} servers")
    except Exception as exc:
        print(f"[chat_ga] ops MCP discovery failed: {exc}")
        _OPS_TOOLS_SCHEMAS = []
    return _OPS_TOOLS_SCHEMAS, _OPS_TOOLS_NAME_MAP


def _call_graph_mcp_sync(tool: str, args: dict) -> str:
    """Sync version of the JSON-RPC dance in mcp_tool_call.

    Returns a stringified result. Errors are returned as readable
    text rather than raised — the agent loop should keep going after
    a soft tool failure.
    """
    if tool not in _GRAPH_MCP_ALLOWED:
        return f"error: tool {tool!r} not in graph_rag allowlist"
    cmd = [
        os.environ.get(
            "AIFORGE_MCP_BIN",
            "/home/mani/AIForgeCrew/.venv/bin/aiforge-graph-mcp",
        )
    ]
    env = {
        **os.environ,
        "AIFORGE_NEO4J_URI": os.environ.get(
            "AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687"),
        "AIFORGE_NEO4J_USER": os.environ.get("AIFORGE_NEO4J_USER", "neo4j"),
        "AIFORGE_NEO4J_PASSWORD": os.environ.get(
            "AIFORGE_NEO4J_PASSWORD", "password"),
        "AIFORGE_DSN": os.environ.get(
            "AIFORGE_DSN",
            "postgresql://aiforge:aiforgepass@127.0.0.1:5432/aiforge"),
    }
    init_req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {}},
                           "clientInfo": {"name": "aiforge-chat-ga", "version": "0.1"}}}
    init_notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    tool_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": tool, "arguments": args}}
    payload = (
        json.dumps(init_req) + "\n" +
        json.dumps(init_notify) + "\n" +
        json.dumps(tool_req) + "\n"
    )
    import subprocess as _sp
    try:
        proc = _sp.run(
            cmd, input=payload.encode(),
            capture_output=True, timeout=30, check=False, env=env,
        )
    except FileNotFoundError:
        return f"error: MCP binary not found: {cmd[0]}"
    except _sp.TimeoutExpired:
        return f"error: MCP {tool} timed out after 30s"
    for line in proc.stdout.splitlines():
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if isinstance(msg, dict) and msg.get("id") == 2:
            if "error" in msg:
                return f"error: {msg['error']}"
            return json.dumps(msg.get("result"), ensure_ascii=False)[:4000]
    return f"error: no MCP response (stderr={proc.stderr[:200]!r})"


def _chat_ga_cfg() -> dict:
    """Build a GA LLMSession cfg from the persisted chat-role config.

    Mirrors doer.ga_tools.llm_config.primary_cfg shape so GA's
    LLMSession + ToolClient consume it as-is. Honours per-role
    overrides set via the Settings UI (provider + model).
    """
    from aiforge_core.runtime import agent_config as _acfg
    k = _acfg.resolve_litellm("chat")
    model_id = k["model_id"]
    # Strip LiteLLM provider prefix — GA passes the model verbatim to
    # the OpenAI-compat /chat/completions endpoint and refuses
    # 'openai/llama3.1:70b' as a model name.
    for prefix in ("openai/", "ollama/"):
        if model_id.startswith(prefix):
            model_id = model_id[len(prefix):]
            break
    apibase = (k["api_base"] or "").rstrip("/")
    if apibase.endswith("/v1"):
        apibase = apibase[:-3]
    return {
        "name": "ga-chat",
        "apikey": k["api_key"] or "sk-local",
        "apibase": apibase,
        "model": model_id,
        "api_mode": "chat_completions",
        # Cloud OpenAI-compat endpoints (Ollama Cloud, Gemini)
        # interleave `delta.reasoning` with `delta.tool_calls` chunks
        # GA's SSE parser doesn't reassemble. Force non-stream so we
        # parse one full JSON response with structured tool_calls.
        # Local mlx-lm tolerates either; non-stream is universally safe.
        "stream": False,
        "max_retries": 2,
        "connect_timeout": 10,
        "read_timeout": 180,
        "context_win": int(os.environ.get("AIFORGE_CHAT_CTX", "32000")),
        "max_tokens": int(os.environ.get("AIFORGE_CHAT_MAX_TOKENS", "2048")),
        "temperature": float(os.environ.get("AIFORGE_CHAT_TEMP", "0.1")),
    }


def _chat_via_ga(query: str) -> dict:
    """Run the chat agent through GA's text-protocol agent loop.

    Returns ``{answer, trace}`` matching the old smolagents shape.
    """
    from . import otel as _otel
    with _otel.span("chat.via_ga", role="chat", query_len=len(query)):
        return _chat_via_ga_inner(query)


def _chat_via_ga_inner(query: str) -> dict:
    from aiforge_core.doer.ga_compat import import_ga, ParentShim
    try:
        ga = import_ga()
    except Exception as exc:
        return {"answer": f"GA import failed: {exc}", "trace": []}

    LLMSession = ga["LLMSession"]
    ToolClient = ga["ToolClient"]
    agent_runner_loop = ga["agent_runner_loop"]
    StepOutcome = ga["StepOutcome"]
    BaseHandlerAgentLoop = None
    try:
        # BaseHandler lives next to agent_runner_loop in agent_loop.py
        # — pull it via the same module the import_ga prepended to
        # sys.path. Safe to import lazily here.
        from agent_loop import BaseHandler as BaseHandlerAgentLoop  # type: ignore
    except Exception as exc:
        return {"answer": f"GA BaseHandler import failed: {exc}", "trace": []}

    # Memory writer used by both search_memory and the auto-retain
    # post-step. Single instance per call keeps the Neo4j driver warm.
    from .memory import Memory
    mem = Memory()

    captured: dict[str, Any] = {"answer": None, "trace": []}

    class _ChatHandler(BaseHandlerAgentLoop):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            self.parent = ParentShim(
                task_dir=os.path.join("/tmp", f"aiforge-chat-{int(time.time()*1000)}")
            )
            try:
                os.makedirs(self.parent.task_dir, exist_ok=True)
            except Exception:
                pass
            self._done_hooks: list[str] = []
            self.max_turns = 12
            self.current_turn = 0

        def _record(self, name: str, args: dict, *,
                    data: str | None = None) -> None:
            captured["trace"].append({
                "tool": name, "args": args,
                "last_data": data,
            })

        def do_unified_memory_query(self, args, response):
            from aiforge_core.memory.unified_query import query as _uq, render as _ur
            q = (args.get("query") or "").strip()
            ticket = (args.get("ticket") or None) or None
            role = args.get("role") or "sr_developer"
            limit = int(args.get("limit") or 8)
            try:
                result = _uq(q, ticket=ticket, role=role, limit=limit)
                rendered = _ur(result)
            except Exception as exc:
                rendered = f"unified_memory_query error: {exc}"
            self._record("unified_memory_query",
                         {"query": q, "ticket": ticket,
                          "role": role, "limit": limit},
                         data=rendered)
            yield ""
            return StepOutcome(
                data=rendered, next_prompt="continue", should_exit=False,
            )

        def do_search_memory(self, args, response):
            q = (args.get("query") or "").strip()
            role = args.get("role") or "sr_developer"
            top_k = int(args.get("top_k") or 5)
            try:
                hits = mem.search(q, role=role, top_k=top_k)
                lines = [
                    f"[{h.tier}/{h.wing}] {h.text[:300].replace(chr(10), ' ')}"
                    for h in hits
                ] or ["no hits"]
                blob = "\n".join(lines)
            except Exception as exc:
                blob = f"search_memory error: {exc}"
            self._record("search_memory",
                         {"query": q, "role": role, "top_k": top_k},
                         data=blob)
            yield ""
            return StepOutcome(
                data=blob, next_prompt="continue", should_exit=False,
            )

        def do_final_answer(self, args, response):
            ans = (args.get("answer") or "").strip()
            captured["answer"] = ans or "(empty answer)"
            self._record("final_answer", {"answer_len": len(ans)})
            yield ""
            return StepOutcome(data=None, next_prompt=None, should_exit=True)

        def __getattr__(self, name):
            # Dynamic dispatch for graph_rag MCP tools — every entry in
            # _GRAPH_MCP_ALLOWED exposes a do_<name> on demand without
            # needing an explicit method per tool. Anything outside the
            # allowlist raises AttributeError → BaseHandler.dispatch
            # falls through to the "未知工具" branch which gracefully
            # nudges the model with the actual tool name.
            if not name.startswith("do_"):
                raise AttributeError(name)
            tname = name[3:]
            ops_map = getattr(self, "_ops_name_map", {}) or {}
            if tname in _GRAPH_MCP_ALLOWED:
                origin = ("graph", tname)
            elif tname in ops_map:
                url, raw_tool = ops_map[tname]
                origin = ("ops", url, raw_tool)
            else:
                raise AttributeError(name)
            handler_self = self

            def _gen(args, response):
                clean = {k: v for k, v in args.items() if k != "_index"}
                if origin[0] == "graph":
                    result = _call_graph_mcp_sync(origin[1], clean)
                else:
                    from .mcp_http import call_tool as _ops_call
                    result = _ops_call(origin[1], origin[2], clean)
                handler_self._record(tname, clean, data=result)
                yield ""
                return StepOutcome(
                    data=result, next_prompt="continue", should_exit=False,
                )
            return _gen

    # Ops MCP tools (mongo / k8s / tekton / tally) — discovered once
    # per process. Schemas extend the model's tools list; dispatch
    # below routes ``ops_<server>_<tool>`` calls via mcp_http.
    ops_schemas, ops_name_map = _ops_tool_schemas()
    extended_schema = list(_CHAT_TOOLS_SCHEMA) + ops_schemas

    handler = _ChatHandler()
    handler._ops_name_map = ops_name_map  # type: ignore[attr-defined]
    cfg = _chat_ga_cfg()
    try:
        session = LLMSession(cfg=cfg)
        # Stamp provider-aware prompt-cache markers on outgoing
        # messages — Anthropic ephemeral, OpenAI prefix-cache,
        # Gemini stub. Idempotent.
        try:
            from aiforge_core.runtime import agent_config as _acfg
            _provider = _acfg.get("chat").get("provider", "local")
            from aiforge_core.llm import cache_markers as _cm
            _cm.apply_to_session(session, provider=_provider)
        except Exception as _exc:
            print(f"[chat] cache_markers wiring skipped: {_exc}")
        client = ToolClient(session)
    except Exception as exc:
        return {"answer": f"LLM session build failed: {exc}", "trace": []}

    try:
        # exhaust the generator — we don't stream chat to the client
        # yet; just collect the final captured answer.
        gen = agent_runner_loop(
            client,
            system_prompt=_CHAT_GA_PREAMBLE,
            user_input=f"## Question\n{query}",
            handler=handler,
            tools_schema=extended_schema,
            max_turns=12,
            verbose=False,
        )
        for _ in gen:
            pass
    except Exception as exc:
        return {"answer": f"GA loop error: {exc}", "trace": captured["trace"]}

    if not captured["answer"]:
        # Fallback A: prefer the LAST successful tool result.
        # When max_turns trips after a useful tool call, the model
        # often slips into "未知工具 no_tool" cycles whose assistant
        # text is hallucinated commentary on the GA placeholder. The
        # tool_result content is the actual data the model was
        # supposed to summarise — surface that instead.
        last_result = None
        for entry in reversed(captured.get("trace") or []):
            tool = entry.get("tool")
            if tool in (None, "no_tool", "final_answer"):
                continue
            data = entry.get("last_data")
            if isinstance(data, str) and data.strip():
                last_result = (
                    f"[partial · {tool}]\n{data[:1800]}"
                )
                break
        if last_result:
            captured["answer"] = last_result
    if not captured["answer"]:
        # Fallback B: scrape last assistant text from GA session.
        try:
            for msg in reversed(getattr(session, "history", []) or []):
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            text += blk.get("text") or ""
                text = (text or "").strip()
                # Skip hallucinated commentary on GA placeholder.
                if "未知工具" in text or text.startswith("<thinking>"):
                    continue
                if text and not text.startswith("!!!Error:"):
                    captured["answer"] = text[:2000]
                    break
        except Exception:
            pass
    if not captured["answer"]:
        captured["answer"] = (
            "(no final_answer emitted — model exhausted turns or stalled)"
        )
    # Cost accounting — best-effort. GA's BaseSession exposes total
    # tokens via per-session counters when present (older GA versions
    # don't, hence the getattr fallback).
    try:
        from . import cost as _cost
        prompt_tokens = int(getattr(session, "total_prompt_tokens", 0) or 0)
        completion_tokens = int(
            getattr(session, "total_completion_tokens", 0) or 0,
        )
        _cost.record_call(
            role="chat", ticket=None, model=cfg["model"],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    except Exception:
        pass
    return {"answer": captured["answer"], "trace": captured["trace"]}


def _auto_retain_chat(query: str, answer: str, trace: list[dict]) -> str | None:
    """Persist the Q+A as a T3 chat_qa fact for next-time retrieval.

    Best-effort — never raises. Returns the new memory id (or None on
    failure) so the API can echo it for debugging.
    """
    if not query.strip() or not answer.strip():
        return None
    if answer.startswith(("(no final_answer", "GA ", "LLM session")):
        # Don't pollute memory with our own error stubs.
        return None
    try:
        from .memory import Memory
        mem = Memory()
        text = (
            f"Chat Q+A · auto\n"
            f"Q: {query[:400]}\n"
            f"A: {answer[:1500]}"
        )
        return mem.retain_fact(  # type: ignore[return-value]
            text=text,
            tier="t3",
            wing="patterns/chat-auto",
            kind="chat_qa",
            source="chat-ga",
            metadata={
                "auto": True,
                "tools_called": [t.get("tool") for t in trace][:10],
            },
        )
    except Exception as exc:
        print(f"[chat] auto-retain failed: {exc}")
        return None


@app.post("/api/chat/ask")
def chat_ask(body: _ChatAskBody) -> dict:
    """LLM answer grounded in Neo4j memory + live MCP tool access.

    Runs a GA (GenericAgent) text-protocol loop via ``_chat_via_ga``.
    The model picks tools from: unified_memory_query (preferred,
    merges 5 sources), graph_rag MCP allowlist (sym_lookup, impact,
    ticket_brief, ...), and ops MCPs (mongo / k8s / tekton / tally).
    Normalize pass cleans typos before the agent sees the query.
    """
    normalized = _normalize_query(body.query)

    use_agent = os.environ.get("AIFORGE_CHAT_AGENT", "1") == "1"
    if use_agent:
        try:
            agent_out = _chat_via_ga(normalized)
        except Exception as exc:
            raise HTTPException(502, f"Chat agent failed: {exc}")
        # Pull a compact hit list from memory.search for drawer display.
        from .memory import Memory
        try:
            hits = Memory().search(normalized, role=body.role, top_k=body.top_k)
        except Exception:
            hits = []
        # Auto-learn: every successful chat answer becomes a T3 chat_qa
        # fact so the next similar query benefits from this synthesis
        # without operator confirmation. Disable with
        # AIFORGE_CHAT_AUTORETAIN=0.
        retained_id = None
        if os.environ.get("AIFORGE_CHAT_AUTORETAIN", "1") == "1":
            retained_id = _auto_retain_chat(
                normalized, agent_out["answer"], agent_out.get("trace") or [],
            )
        return {
            "query": body.query,
            "normalized": normalized if normalized != body.query else None,
            "answer": agent_out["answer"],
            "tiers_used": sorted({
                getattr(h, "tier", None) or
                (h.get("tier") if isinstance(h, dict) else "?")
                for h in hits
            }),
            "hits": [
                {"tier": getattr(h, "tier", None) or (h.get("tier") if isinstance(h, dict) else "?"),
                 "wing": getattr(h, "wing", None) or (h.get("wing") if isinstance(h, dict) else "?"),
                 "text": (getattr(h, "text", None) or (h.get("text") if isinstance(h, dict) else ""))[:200]}
                for h in hits[:10]
            ],
            "tools_called": agent_out.get("trace") or [],
            "auto_retained_id": retained_id,
        }

    ctx = _collect_chat_context(normalized, body.role, body.top_k)
    prompt = _build_chat_prompt(normalized, ctx)
    try:
        answer = _call_llm_chat(prompt)
    except Exception as exc:
        raise HTTPException(502, f"LLM call failed: {exc}")
    return {
        "query": body.query,
        "normalized": normalized if normalized != body.query else None,
        "answer": answer,
        "tiers_used": sorted({getattr(h, "tier", None) or
                              (h.get("tier") if isinstance(h, dict) else "?")
                              for h in ctx["hits"]}),
        "hits": [
            {"tier": getattr(h, "tier", None) or (h.get("tier") if isinstance(h, dict) else "?"),
             "wing": getattr(h, "wing", None) or (h.get("wing") if isinstance(h, dict) else "?"),
             "text": (getattr(h, "text", None) or (h.get("text") if isinstance(h, dict) else ""))[:200]}
            for h in ctx["hits"][:10]
        ],
        "tools_called": [
            {"tool": m["tool"], "args": m["args"]} for m in ctx["mcp"]
        ],
    }


# ─────────────────────────── Chat flow retention ────────────────────────
#
# When the operator confirms a chat answer worked, persist it as a T3
# skill so the next similar query hits it via `memory.search` /
# `related_memories`. Building the "flow library" automatically.


class _ChatRetainBody(BaseModel):
    query: str = Field(..., description="Operator's original question")
    answer: str = Field(..., description="Short summary of what worked")
    worked: bool = Field(True, description="False = negative lesson, still useful")
    topic: str = Field("general", description="Short topic slug, e.g. 'pagination'")
    hit_refs: list[str] = Field(
        default_factory=list,
        description="Optional list of memory ids or ticket identifiers used",
    )


@app.post("/api/chat/retain", status_code=201)
def chat_retain(body: _ChatRetainBody) -> dict:
    """Persist a chat Q+A into T3 memory (`patterns/<topic>`).

    We write both the positive and the negative case — a validated
    non-answer is still useful ("tried X, didn't help for Y"). The
    learner-style format keeps it short: one-line summary, one-line
    query echo, hit refs inline.
    """
    from .memory import Memory
    topic = (body.topic or "general").strip().lower().replace(" ", "-")[:40]
    marker = "✔ worked" if body.worked else "✘ did not help"
    text = (
        f"Chat flow · {topic} · {marker}\n"
        f"Q: {body.query[:400]}\n"
        f"A: {body.answer[:800]}"
    )
    if body.hit_refs:
        text += "\nrefs: " + ", ".join(body.hit_refs[:10])
    mem = Memory()
    rid = mem.retain_fact(
        text=text, tier="t3",
        wing=f"patterns/{topic}",
        kind="chat_flow",
        source="chat-ui",
        metadata={
            "topic": topic,
            "worked": body.worked,
            "hit_refs": body.hit_refs[:10],
        },
    )
    return {"id": rid, "tier": "t3", "wing": f"patterns/{topic}",
            "worked": body.worked}


# ─────────────────────────── MCP bridge ─────────────────────────────────
#
# Exposes the graph_rag MCP tools over HTTP so the React dashboard can
# fire sym_lookup / impact / cross_repo_flow / related_memories / etc.
# without needing its own stdio MCP client. Spawns the
# ``aiforge-graph-mcp`` console script per call and speaks JSON-RPC via
# subprocess stdin/stdout. Tool allowlist prevents arbitrary-method abuse.

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
@app.get("/api/workflow/topology")
def workflow_topology(ticket: str | None = None) -> dict:
    """DAG snapshot for the UI graph view. Optional ?ticket=X overlays
    per-node status + last_event_at."""
    from . import workflow_topology as _wt
    return _wt.snapshot(ticket)


@app.get("/api/workflow/stream")
def workflow_stream(ticket: str | None = None,
                    interval: int = 3) -> StreamingResponse:
    """SSE topology refresh. Emits one snapshot every ``interval``
    seconds (clamped 1..30). UI ``EventSource`` consumes for live
    DAG status. Disconnect-safe — generator exits when client closes.
    """
    from . import workflow_topology as _wt
    interval = max(1, min(int(interval or 3), 30))

    def _gen():
        import time as _t
        while True:
            snap = _wt.snapshot(ticket)
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
    from . import cost as _cost
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
    from . import repo_standards as _rs
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
    from . import repo_standards as _rs
    std = _rs.upsert(name, **{k: v for k, v in body.model_dump().items()
                              if v is not None})
    return repo_standards_get(name=name)


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
                return await super().get_response(path, scope)
            except Exception:
                return FileResponse(os.path.join(_DIST, "index.html"))

    app.mount("/ui", _SpaStatic(directory=_DIST, html=True), name="ui")

    @app.get("/")
    def _root_redirect():
        return FileResponse(os.path.join(_DIST, "index.html"))
else:
    @app.get("/")
    def _root_info() -> dict:
        return {
            "service": "aiforge api",
            "hint": "run `cd web && npm run build` to serve the UI at /ui/",
            "routes": [r.path for r in app.routes if hasattr(r, "path")],
        }
