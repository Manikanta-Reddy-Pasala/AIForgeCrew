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
    return {
        "id": r["id"], "identifier": r["identifier"], "title": r["title"],
        "body": r["body"], "status": r["status"], "priority": r["priority"],
        "assignee_role": _cfg.canonical_role(r["assignee_role"]) if r.get("assignee_role") else None,
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
    q = (
        "SELECT tickets.*, "
        "(SELECT MIN(created_at) FROM ticket_events "
        " WHERE ticket_id=tickets.id AND kind='status_change' AND body='in_progress'"
        ") AS started_at "
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
    with _db() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT tickets.*, {_started_expr} FROM tickets WHERE identifier=%s",
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
            f"SELECT tickets.*, {_started_expr} FROM tickets "
            "WHERE parent_id=%s ORDER BY created_at ASC",
            (ticket_id,),
        )
        children = [_ticket_row_out(r) for r in cur.fetchall()]
    return {
        "ticket": _ticket_row_out(t),
        "events": events,
        "children": children,
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
    import urllib.request
    try:
        payload = json.dumps({
            "model": os.environ.get(
                "AIFORGE_CHAT_NORMALIZE_MODEL",
                os.environ.get("AIFORGE_PLANNER_MODEL", "qwen3.6-27b"),
            ),
            "messages": [
                {"role": "system", "content": _NORMALIZE_SYSTEM},
                {"role": "user", "content": q[:600]},
            ],
            "max_tokens": 128,
            "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode()
        req = urllib.request.Request(
            f"{LM_STUDIO_BASE_URL}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {_cfg.LM_STUDIO_API_KEY}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        content = ((body.get("choices") or [{}])[0]
                   .get("message", {}).get("content") or "").strip()
        if not content:
            return q
        # Strip any stray quoting / leading labels from the model.
        content = content.strip('"\' ')
        for prefix in ("normalized:", "query:", "rewritten:"):
            if content.lower().startswith(prefix):
                content = content[len(prefix):].strip()
        return content[:300] or q
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
    """One-shot LLM call against LM Studio for chat synthesis."""
    import urllib.request
    payload = json.dumps({
        "model": os.environ.get(
            "AIFORGE_CHAT_MODEL",
            os.environ.get("AIFORGE_PLANNER_MODEL", "qwen3.6-27b"),
        ),
        "messages": [
            {"role": "system", "content": _CHAT_SYSTEM},
            {"role": "user", "content": prompt[:30_000]},
        ],
        "max_tokens": 2048,
        "temperature": 0.1,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        f"{LM_STUDIO_BASE_URL}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_cfg.LM_STUDIO_API_KEY}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())
    msg = (body.get("choices") or [{}])[0].get("message", {}) or {}
    content = (msg.get("content") or "").strip()
    if content:
        return content
    return (msg.get("reasoning_content") or "").strip() or "(empty reply)"


def _chat_agent_answer(query: str) -> dict:
    """Run a smolagents CodeAgent with the full 25-tool graph_rag MCP
    set. Model decides which tools to call (sym_lookup, impact,
    cross_repo_flow, caller_chain, ...) — no pre-fetch heuristics.
    """
    import os as _os
    _os.environ.setdefault("AIFORGE_GRAPH_MCP_ENABLED", "1")

    from smolagents import CodeAgent, LiteLLMModel
    from aiforge_core.legacy.mcp_graph import graph_rag_tools
    from aiforge_core.planner.tools import make_search_memory

    # Per-agent provider+model lookup via agent_config (persisted JSON).
    # Env overrides still win inside resolve_litellm when set.
    kwargs = _acfg.resolve_litellm("chat")
    model = LiteLLMModel(
        model_id=kwargs["model_id"],
        api_base=kwargs["api_base"],
        api_key=kwargs["api_key"],
        max_tokens=16384,
        temperature=0.1,
    )

    ctx = {"ticket": None, "worktree_root": "~/codeRepo",
           "store": None, "log": None}
    tools = [make_search_memory(ctx)]
    try:
        tools.extend(graph_rag_tools())
    except Exception as exc:
        return {"answer": f"MCP load failed: {exc}", "trace": []}

    # OneShell ops MCP servers (mongo / k8s / tekton / tally) expose
    # their tools over streamable-http on :8810-:8813. smolagents
    # ToolCollection.from_mcp is a context manager — keep every MCP
    # alive for the duration of the agent.run() call via an ExitStack.
    # Per-server failures are soft.
    from contextlib import ExitStack
    from smolagents import ToolCollection

    ops_servers = {
        "oneshell_mongo":  _os.environ.get("AIFORGE_MCP_MONGO",  "http://127.0.0.1:8810/mcp"),
        "oneshell_k8s":    _os.environ.get("AIFORGE_MCP_K8S",    "http://127.0.0.1:8811/mcp"),
        "oneshell_tekton": _os.environ.get("AIFORGE_MCP_TEKTON", "http://127.0.0.1:8812/mcp"),
        "oneshell_tally":  _os.environ.get("AIFORGE_MCP_TALLY",  "http://127.0.0.1:8813/mcp"),
    }
    stack = ExitStack()
    loaded: list[str] = []
    # Seed seen-set with names already claimed by graph_rag + search_memory
    # so an ops-server tool with the same name (list_services,
    # find_business, ...) doesn't duplicate. Ops tools also get a short
    # prefix when they'd clash, so the model can still reach them.
    seen: dict[str, str] = {getattr(t, "name", str(i)): "core"
                            for i, t in enumerate(tools)}
    for name, url in ops_servers.items():
        prefix = name.replace("oneshell_", "") + "_"
        try:
            tc = stack.enter_context(ToolCollection.from_mcp(
                {"url": url}, trust_remote_code=True,
            ))
            kept = 0
            for t in tc.tools:
                tname = getattr(t, "name", None) or ""
                if tname in seen:
                    # Rename to avoid smolagents "duplicate tool name" error.
                    newname = prefix + tname
                    if newname in seen:
                        continue
                    try:
                        t.name = newname
                    except Exception:
                        continue
                    tname = newname
                seen[tname] = name
                tools.append(t)
                kept += 1
            loaded.append(f"{name}:{kept}")
        except Exception as exc:
            print(f"[chat_agent] skipped {name} at {url}: {exc}")
    if loaded:
        print(f"[chat_agent] ops MCPs loaded: {loaded}, total tools={len(tools)}")

    _CHAT_AGENT_PREAMBLE = """You are the AIForge chat agent. The
operator asked a question about our OneShell codebase / past
tickets / decisions. You have live access to the Neo4j graph, T1-T4
memory, and 25 graph_rag MCP tools (sym_lookup, impact,
cross_repo_flow, caller_chain, callee_chain, read_source,
ticket_brief, related_memories, find_doc, list_services, list_repos,
list_endpoints, graph_neighborhood, data_lineage, build_plan,
test_plan, kube_status, etc). Use them.

Rules:
- Call 1-4 tools that actually relate to the query. Do not call
  everything. Start narrow (sym_lookup / ticket_brief /
  related_memories), expand only if the first hit is thin.
- Cite file:line when you quote code and [ticket-id] when you quote
  a ticket.
- If the tools return nothing useful, say so in one line; do not
  hallucinate file paths or symbols.

Reply format: 1-2 line direct answer, then a short bullet list of the
concrete evidence you used (tool → what it returned → conclusion).
"""

    agent = CodeAgent(
        tools=tools,
        model=model,
        max_steps=8,
        additional_authorized_imports=["json", "re"],
    )
    task = f"{_CHAT_AGENT_PREAMBLE}\n\n## Question\n{query}"
    try:
        # Keep the MCP session open while the agent is running.
        raw = agent.run(task)
    except Exception as exc:
        stack.close()
        return {"answer": f"agent error: {exc}", "trace": []}
    finally:
        # Ensure MCP clients close even on success.
        try: stack.close()
        except Exception: pass

    # smolagents agent.run returns the final_answer payload (may be
    # str or dict). Stringify safely.
    answer = raw if isinstance(raw, str) else json.dumps(raw, default=str)
    tools_called: list[dict] = []
    try:
        for step in getattr(agent, "memory", None).steps if hasattr(agent, "memory") else []:
            for tc in getattr(step, "tool_calls", []) or []:
                tools_called.append({
                    "tool": getattr(tc, "name", "?"),
                    "args": getattr(tc, "arguments", {}) or {},
                })
    except Exception:
        pass
    return {"answer": answer, "trace": tools_called}


@app.post("/api/chat/ask")
def chat_ask(body: _ChatAskBody) -> dict:
    """LLM answer grounded in Neo4j memory + live MCP tool access.

    Runs a smolagents CodeAgent loop — the model picks which of the
    25 graph_rag MCP tools to call (sym_lookup, impact, ticket_brief,
    ...) instead of the old heuristic pre-fetch. Normalize pass still
    cleans typos before the agent sees the query.
    """
    normalized = _normalize_query(body.query)

    use_agent = os.environ.get("AIFORGE_CHAT_AGENT", "1") == "1"
    if use_agent:
        try:
            agent_out = _chat_agent_answer(normalized)
        except Exception as exc:
            raise HTTPException(502, f"Chat agent failed: {exc}")
        # Pull a compact hit list from memory.search for drawer display.
        from .memory import Memory
        try:
            hits = Memory().search(normalized, role=body.role, top_k=body.top_k)
        except Exception:
            hits = []
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
