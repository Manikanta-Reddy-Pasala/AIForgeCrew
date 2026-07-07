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
import hmac
import ipaddress
import json
import logging
import os
import re
import threading
from datetime import UTC
from typing import Any

import psycopg
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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

# Make the aiforge.* logger family visible regardless of uvicorn's default
# config so diagnostics (e.g. the provider-test probe) actually print.
# Level via AIFORGE_LOG_LEVEL (default INFO). Guarded against double-add on
# test reloads.
_af_log = logging.getLogger("aiforge")
_af_log.setLevel(getattr(logging, os.environ.get("AIFORGE_LOG_LEVEL", "INFO").upper(), logging.INFO))
if not any(getattr(h, "_aiforge_diag", False) for h in _af_log.handlers):
    _h = logging.StreamHandler()
    _h._aiforge_diag = True  # type: ignore[attr-defined]
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _af_log.addHandler(_h)
    _af_log.propagate = False

app = FastAPI(title="AIForge API")


@app.on_event("startup")
def _guard_and_announce_backends() -> None:
    """FIRST boot step: in data-driven mode (AIFORGE_REQUIRE_DATA_BACKEND=1)
    abort LOUD if any data store still resolves to embedded SQLite, then log
    one line naming every backend. The guard is intentionally hard-fail; the
    log is soft (never crashes boot)."""
    from aiforge_core.config import backends
    backends.require_data_backends()   # hard-fail on misconfigured data mode
    backends.boot_log()                # soft one-line announcement


@app.on_event("startup")
def _ensure_skill_workflow_dirs() -> None:
    """Create the skills + workflows folders on boot so they exist for the
    operator (and the agent) to add ``SKILL.md`` / ``WORKFLOW.md`` files into."""
    try:
        from aiforge_core.runtime import workflows
        workflows.ensure_dirs()
    except Exception:  # noqa: BLE001
        pass


@app.on_event("startup")
def _load_runtime_env() -> None:
    """Restore UI-persisted toggles (runtime.env) into the process env on boot
    using a plain KEY=VALUE parser — NOT a shell source — so a value can never
    be executed. A real env var / project .env already in the environment WINS
    (setdefault), keeping them the operator's explicit escape hatch."""
    try:
        path = _RUNTIME_ENV_PATH
        if not os.path.isfile(path):
            return
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                if k and k not in os.environ:        # don't clobber real env/.env
                    os.environ[k] = v.strip()
    except Exception:  # noqa: BLE001
        pass


@app.on_event("startup")
def _ensure_model_context_on_boot() -> None:
    """Post-deploy, LM Studio JIT-loads the local model at its small default
    context (e.g. 8192), which HTTP-400s the big prompts a multi-file build needs
    — the recurring `llm.exhausted`. On boot, in a background thread, query the
    loaded model(s) and reload any below the target context. Model-agnostic;
    best-effort; AIFORGE_NO_CTX_RELOAD=1 skips, AIFORGE_LM_CONTEXT sets target."""
    if os.environ.get("AIFORGE_NO_CTX_RELOAD"):
        return
    import threading

    def _work():
        try:
            import time as _t
            import urllib.request as _u
            import json as _j
            _t.sleep(8)                       # let the server + LM Studio settle
            try:
                want = int(os.environ.get("AIFORGE_LM_CONTEXT", "262144"))
            except ValueError:
                want = 262144
            base = os.environ.get("AIFORGE_LM_BASE_URL",
                                  "http://127.0.0.1:1234/v1").rstrip("/")
            api0 = base.rsplit("/v1", 1)[0] + "/api/v0/models"
            data = _j.loads(_u.urlopen(api0, timeout=8).read())
            loaded = [(m.get("id"), m.get("loaded_context_length") or 0)
                      for m in data.get("data", []) if m.get("state") == "loaded"]
            below = [mid for mid, ctx in loaded if mid and ctx < want]
            if not below:
                return
            from aiforge_core.runtime import local_starter
            for mid in below:
                try:
                    local_starter.load_model_now(mid, want, ttl=43200)
                    _af_log.info("boot ctx-reload: %s -> %d", mid, want)
                except Exception as _e:  # noqa: BLE001
                    _af_log.debug("boot ctx-reload failed for %s: %s", mid, _e)
        except Exception as _exc:  # noqa: BLE001 — never break boot
            _af_log.debug("boot ctx-reload skipped: %s", _exc)

    threading.Thread(target=_work, name="ctx-reload", daemon=True).start()


@app.on_event("startup")
def _start_jobs_scheduler() -> None:
    """Scheduled-jobs tick loop — daemon thread, same pattern as the
    other background workers. AIFORGE_JOBS_DISABLE=1 skips it."""
    try:
        import threading

        from aiforge_core.jobs import scheduler as jobs_scheduler
        if jobs_scheduler._disabled():
            return
        threading.Thread(target=jobs_scheduler.run_loop,
                         daemon=True, name="jobs-scheduler").start()
    except Exception:  # noqa: BLE001 — startup must never crash the API
        pass


# ─────────────────────── API auth + bind-host guard ─────────────────────
# This control plane RUNS SHELL and EDITS FILES over HTTP, so exposing it
# unauthenticated is a remote-code-execution surface. Design (pragmatic, must
# not break local dev / the UI / the tests):
#   * AIFORGE_API_TOKEN set  → every /api/* route (except health) requires a
#     matching ``Authorization: Bearer <token>`` (or ``X-AIForge-Token``).
#   * token unset + LOOPBACK bind → open (preserves local dev + the UI on
#     localhost + TestClient, which has no real host → treated as loopback).
#   * NON-loopback bind + no token → REFUSE TO BOOT (see _security_boot_guard).
# The UI static assets, ``/files`` and ``/`` stay open (no token) so the app
# shell can load; the browser then sends the operator-configured token on API
# calls. A single shared token — not user accounts. Keep it simple.


def _api_token() -> str:
    return os.environ.get("AIFORGE_API_TOKEN", "").strip()


def _bind_host() -> str:
    """The host uvicorn binds to, surfaced to the app via AIFORGE_BIND_HOST
    (set by run.sh / docker-compose). Defaults to loopback so a bare
    ``uvicorn ...`` / TestClient run is treated as local-open."""
    return (os.environ.get("AIFORGE_BIND_HOST") or "127.0.0.1").strip() or "127.0.0.1"


def _is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if h in ("", "localhost", "127.0.0.1", "::1"):
        return True
    if h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _security_boot_guard() -> None:
    """Refuse to boot when binding a shell-running control plane to a
    non-loopback host without a token. Raises ``RuntimeError`` — called from a
    startup hook AND directly unit-testable."""
    token = _api_token()
    host = _bind_host()
    # Escape hatch: the operator fronts the api with their OWN access layer
    # (Cloudflare Access / a WireGuard-only reverse proxy / nginx auth) and
    # accepts responsibility for exposure. Explicit opt-out so a bind to a
    # tunnel/LAN interface works without the app requiring a token.
    fronted = os.environ.get("AIFORGE_ALLOW_UNAUTH_NONLOOPBACK", "").strip().lower() \
        in ("1", "true", "yes", "on")
    if not _is_loopback_host(host) and not token and not fronted:
        raise RuntimeError(
            f"AIForge refuses to boot: binding to a non-loopback host ({host}) "
            "exposes a shell-running control plane. Set AIFORGE_API_TOKEN to a "
            "shared secret (and configure the UI with it), bind 127.0.0.1, OR "
            "set AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1 if you front it yourself "
            "(Cloudflare / WireGuard-only proxy)."
        )
    if not _is_loopback_host(host) and not token and fronted:
        import logging
        logging.getLogger("aiforge.boot").warning(
            "api bound to %s WITHOUT a token (AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1) "
            "— ensure your own access layer (Cloudflare/WireGuard/nginx) fronts it.",
            host)


@app.on_event("startup")
def _enforce_bind_security() -> None:
    _security_boot_guard()


@app.on_event("startup")
def _warn_default_db_creds() -> None:
    """Soft, never-fatal: if the API is bound to a NON-loopback host but the
    Postgres / Neo4j passwords are still the compose defaults, log a loud
    warning. Doesn't hard-fail (could break a user's current run)."""
    try:
        if _is_loopback_host(_bind_host()):
            return
        weak: list[str] = []
        dsn = os.environ.get("AIFORGE_DSN", "") + os.environ.get("AIFORGE_PG_URL", "")
        if ":aiforgepass@" in dsn or os.environ.get("PG_PASSWORD", "") == "aiforgepass":
            weak.append("Postgres")
        neo_pw = os.environ.get("AIFORGE_NEO4J_PASSWORD") or os.environ.get(
            "NEO4J_PASSWORD", "")
        if neo_pw == "password" or os.environ.get("NEO4J_AUTH", "") == "neo4j/password":
            weak.append("Neo4j")
        if weak:
            _af_log.warning(
                "SECURITY: bound to non-loopback host %s with DEFAULT %s "
                "password(s) — change them before LAN exposure.",
                _bind_host(), " + ".join(weak),
            )
    except Exception:  # noqa: BLE001 — a warning must never crash boot
        pass


def _auth_exempt(path: str) -> bool:
    """Routes reachable without a token even when one is configured: health,
    the UI shell / static assets and the root redirect. Everything else under
    ``/api/`` is protected."""
    if path == "/api/health":
        return True
    return not path.startswith("/api/")


def _extract_request_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return (request.headers.get("x-aiforge-token", "") or "").strip()


@app.middleware("http")
async def _require_token(request: Request, call_next):
    token = _api_token()
    if (
        token
        and request.method != "OPTIONS"          # let CORS preflight through
        and not _auth_exempt(request.url.path)
    ):
        supplied = _extract_request_token(request)
        if not (supplied and hmac.compare_digest(supplied, token)):
            return JSONResponse(
                {"detail": "missing or invalid API token"}, status_code=401
            )
    return await call_next(request)


def _cors_origins() -> list[str]:
    """Allowlist from AIFORGE_CORS_ORIGINS (comma-separated); defaults to the
    localhost UI origins. NEVER ``*`` — this control plane mutates state."""
    raw = os.environ.get("AIFORGE_CORS_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return ["http://127.0.0.1:8799", "http://localhost:8799"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
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
        from datetime import datetime
        end_ts = end or datetime.now(UTC)
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        if end_ts.tzinfo is None:
            end_ts = end_ts.replace(tzinfo=UTC)
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

    # Enumerate the REAL archetype list (config.agent_config) — not the 5
    # legacy env.py ROLES — so the page shows enhancer/architect/planner and
    # every other configured agent. Per-role model/provider come from
    # agent_config; max_turns/tool_allowlist only exist for the legacy ROLES
    # (default sensibly when absent). Activity stats default to 0/null for
    # roles that never fired.
    from aiforge_core.config import agent_config as _acfg

    try:
        roles = _acfg.archetypes()
    except Exception:
        roles = list(ROLES.keys())

    # Only the synthetic default is hidden — every real agent (incl. the chat
    # slot + the context/verifier fan-out sub-agents) is shown, grouped.
    roles = [r for r in roles if r != "_default"]

    _DESC = {
        "enhancer": "Cleans the raw request into a clear, unambiguous spec before planning.",
        "architect": "Designs the file/module structure and approach for the spec.",
        "triage": "Routes the work — trivial fast-path vs full pipeline.",
        "planner": "Splits the design into ordered, concrete subtasks.",
        "verifier": "Critiques the plan before code is written (merges the verify_* verdicts).",
        "researcher": "Gathers the codebase/external context the plan needs.",
        "doer": "Writes the actual code and runs the tools that implement each subtask.",
        "refiner": "Polishes the doer's output — cleanup, edge cases — inside the work loop.",
        "feedback": "In-loop reviewer: checks each pass and feeds corrections back.",
        "learner": "Persists durable lessons/memory so future runs start smarter.",
        "verify_correctness": "Axis critic: is the plan/code correct and complete?",
        "verify_scope": "Axis critic: does it stay within the requested scope?",
        "verify_risk": "Axis critic: flags risky, destructive, or fragile changes.",
        "ctx_memory": "Parallel gatherer: pulls relevant past decisions / memory.",
        "ctx_repomap": "Parallel gatherer: builds a map of the repo structure.",
        "ctx_conventions": "Parallel gatherer: extracts the project's coding conventions.",
        "gap_eval": "Research-completeness critic: drives the bounded re-search loop.",
        "live_verifier": "Boots + exercises the built project against a live-verify recipe.",
        "chat": "The dashboard chat assistant's own model slot (independent of the pipeline).",
    }
    _ORCH = {"enhancer", "architect", "planner"}
    _FANOUT = {"ctx_memory", "ctx_repomap", "ctx_conventions",
               "verify_correctness", "verify_scope", "verify_risk",
               "gap_eval", "live_verifier"}

    def _group(r: str) -> str:
        if r == "chat":
            return "chat"
        if r in _ORCH:
            return "orchestrator"
        if r in _FANOUT:
            return "fanout"
        return "pipeline"

    for name in roles:
        rc = ROLES.get(name)
        try:
            cfg = _acfg.get(name)
        except Exception:
            cfg = {}
        model = (cfg.get("model") if isinstance(cfg, dict) else None) \
            or (rc.model if rc else "")
        # "transport" doubles as the provider chip in the UI: legacy roles
        # report their transport; new orchestrator roles report the provider.
        transport = (rc.transport if rc
                     else (cfg.get("provider") if isinstance(cfg, dict) else None)
                     or "openai_compatible")
        last_iso, turns, active = _activity(name)
        out.append({
            "role": name,
            "description": _DESC.get(name, ""),
            "group": _group(name),
            "model": model,
            "transport": transport,
            "max_turns": rc.max_turns if rc else None,
            "tool_allowlist": list(rc.tool_allowlist) if rc else [],
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


class JobPreviewBody(BaseModel):
    instructions: str = Field(..., min_length=1)


class JobCreate(BaseModel):
    name: str = Field(..., min_length=1)
    # min_length=1, not 9 — croniter validates the real shape (and accepts
    # aliases like "@daily" that are shorter than a 5-field expression);
    # a length heuristic would false-reject those.
    cron: str = Field(..., min_length=1)
    ticket_title: str = Field(..., min_length=1)
    ticket_body: str = Field(..., min_length=1)
    project: str | None = None


class JobScriptCreate(BaseModel):
    """Finalize a conversational job-builder session into a scheduled SCRIPT
    job: the approved script body + a cron. The script is written to the local
    jobs folder (~/.aiforge/jobs), NOT the repo — it's user data."""
    name: str = Field(..., min_length=1)
    cron: str = Field(..., min_length=1)
    script: str = Field(..., min_length=1)
    description: str | None = None


class JobPatch(BaseModel):
    name: str | None = None
    cron: str | None = None
    ticket_title: str | None = None
    ticket_body: str | None = None
    project: str | None = None
    enabled: bool | None = None


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


def _ticket_files_base():
    """Stable, PERSISTENT base dir for ticket attachments.

    Must not depend on ``AIFORGE_REPO_ROOT``: the runner rebinds it per
    ticket, AND in Docker it is unset → defaults to ``HOME/aiforge_workspace``,
    which is NOT a mounted volume, so every container recreate wiped uploads
    (the "image not found" 404). Resolution order:
      1. ``AIFORGE_TICKET_FILES_DIR``        explicit override
      2. ``{AIFORGE_CONFIG_DIR}/ticket-files`` (a persistent volume in Docker)
      3. ``{AIFORGE_REPO_ROOT|~/aiforge_workspace}/.aiforge/ticket-files``
         (repo-relative for a local checkout)
    """
    import os as _os
    from pathlib import Path as _Path
    explicit = _os.environ.get("AIFORGE_TICKET_FILES_DIR", "").strip()
    if explicit:
        return _Path(explicit).expanduser().resolve()
    cfg = _os.environ.get("AIFORGE_CONFIG_DIR", "").strip()
    if cfg:
        return (_Path(cfg).expanduser() / "ticket-files").resolve()
    root = _Path(_os.path.expanduser(_os.environ.get(
        "AIFORGE_REPO_ROOT", "~/aiforge_workspace",
    ))).resolve()
    return (root / ".aiforge" / "ticket-files").resolve()


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
    # workspace and stamp metadata.attached_files. The runner materializes
    # them into the per-ticket worktree so the Doer can ``file_read`` them
    # on whatever provider the role is configured for.
    if payload.attached_files:
        attach_meta = _persist_ticket_attachments(t.identifier,
                                                  payload.attached_files)
        if attach_meta:
            patched_md = dict(t.metadata or {})
            patched_md["attached_files"] = attach_meta
            try:
                tickets_mod.update_status(
                    t.id, t.status, role="api",
                    metadata_patch={"attached_files": attach_meta},
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


# ─────────────────────────── scheduled jobs ─────────────────────────

_CRONITER_HINT = ("Scheduled jobs need the 'croniter' package — run "
                  "`uv pip install croniter` (or `uv sync`) and restart the API.")


def _require_croniter() -> None:
    """503 with an actionable message when croniter isn't installed, instead
    of an opaque ModuleNotFoundError 500 that breaks the whole Jobs page."""
    from aiforge_core.jobs import parse as jobs_parse
    if not jobs_parse.CRONITER_AVAILABLE:
        raise HTTPException(503, _CRONITER_HINT)


@app.post("/api/jobs/preview")
def jobs_preview(payload: JobPreviewBody) -> dict:
    """NL instructions → parsed draft + human schedule + next runs.
    Saves NOTHING. Parse errors come back as {ok: False, error} so the
    UI renders them in the preview card instead of a 500."""
    from aiforge_core.jobs import parse as jobs_parse
    if not jobs_parse.CRONITER_AVAILABLE:
        return {"ok": False, "error": _CRONITER_HINT}
    return jobs_parse.parse_instructions(payload.instructions)


@app.post("/api/jobs", status_code=201)
def jobs_create(payload: JobCreate) -> dict:
    from aiforge_core.jobs import parse as jobs_parse, store as jobs_store
    _require_croniter()
    # schedulable() rejects both invalid AND save-valid-but-unschedulable
    # crons (e.g. "0 0 31 2 *"), so next_runs below can't 500.
    if not jobs_parse.schedulable(payload.cron):
        raise HTTPException(400, f"invalid or unschedulable cron: {payload.cron!r}")
    nxt = jobs_parse.next_runs(payload.cron, n=1)[0]
    return jobs_store.create(
        name=payload.name, cron=payload.cron,
        ticket_title=payload.ticket_title, ticket_body=payload.ticket_body,
        project=payload.project, next_run_at=nxt)


@app.post("/api/jobs/script", status_code=201)
def jobs_create_script(payload: JobScriptCreate) -> dict:
    """Finalize a script job: write the approved script to the local jobs
    folder and register a cron job that RUNS it (deterministic — no LLM per
    tick). This is the endpoint the conversational job builder calls once the
    user has dry-run and approved the script."""
    from aiforge_core.jobs import parse as jobs_parse
    from aiforge_core.jobs import scripts as jobs_scripts
    from aiforge_core.jobs import store as jobs_store
    _require_croniter()
    if not jobs_parse.schedulable(payload.cron):
        raise HTTPException(400, f"invalid or unschedulable cron: {payload.cron!r}")
    try:
        script_path = jobs_scripts.write_script(payload.name, payload.script)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    nxt = jobs_parse.next_runs(payload.cron, n=1)[0]
    body = payload.description or f"Runs script: {script_path}"
    # UPSERT by name: re-finalizing a job of the SAME name UPDATES it (new
    # schedule + script) instead of scheduling a SECOND duplicate that fires
    # alongside the original. (The old script file is left on disk — scripts are
    # kept for reuse by design — but only ONE job/schedule points at the new one.)
    existing = next((j for j in jobs_store.list_jobs()
                     if j.get("name") == payload.name
                     and (j.get("kind") or "ticket") == "script"), None)
    if existing:
        return jobs_store.update(
            existing["id"], cron=payload.cron, ticket_body=body,
            next_run_at=nxt, script_path=script_path, last_error=None) or existing
    return jobs_store.create(
        name=payload.name, cron=payload.cron,
        ticket_title=payload.name, ticket_body=body,
        project=None, next_run_at=nxt, kind="script", script_path=script_path)


@app.get("/api/jobs")
def jobs_list() -> list[dict]:
    from aiforge_core.jobs import parse as jobs_parse, store as jobs_store
    out = jobs_store.list_jobs()
    for j in out:
        j["human_schedule"] = jobs_parse.human_schedule(j["cron"])
    return out


@app.patch("/api/jobs/{job_id}")
def jobs_patch(job_id: int, payload: JobPatch) -> dict:
    from aiforge_core.jobs import parse as jobs_parse, store as jobs_store
    if jobs_store.get(job_id) is None:
        raise HTTPException(404, f"job {job_id} not found")
    _require_croniter()
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    # Reject blanking a required text field (JobPatch has no min_length, so
    # {"ticket_title": ""} would otherwise store an empty value that later
    # fires an empty-title ticket).
    for k in ("name", "ticket_title", "ticket_body"):
        if k in fields and not str(fields[k]).strip():
            raise HTTPException(400, f"{k} cannot be empty")
    if "cron" in fields:
        if not jobs_parse.schedulable(fields["cron"]):
            raise HTTPException(400,
                                f"invalid or unschedulable cron: {fields['cron']!r}")
        fields["next_run_at"] = jobs_parse.next_runs(fields["cron"], n=1)[0]
    return jobs_store.update(job_id, **fields)


@app.delete("/api/jobs/{job_id}")
def jobs_delete(job_id: int) -> dict:
    from aiforge_core.jobs import store as jobs_store
    # Deleting the row IS deleting the schedule — the scheduler only ever
    # fires rows `due_jobs()` returns, so a removed row can never fire again
    # (there's no separate OS crontab/systemd entry to also clean up). The
    # script FILE is left on disk on purpose: it's user-authored/approved
    # content the operator may want to reuse for a new job later, not
    # scheduler-owned state.
    if not jobs_store.delete(job_id):
        raise HTTPException(404, f"job {job_id} not found")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/run-now")
def jobs_run_now(job_id: int) -> dict:
    """Manual fire — same code path as the scheduler tick; works even
    when the job is paused."""
    from aiforge_core.jobs import scheduler as jobs_scheduler
    from aiforge_core.jobs import store as jobs_store
    job = jobs_store.get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    _require_croniter()
    ok = jobs_scheduler.fire(job)
    return {"ok": ok, "job": jobs_store.get(job_id)}


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
    if (payload.assignee_role or payload.labels is not None
            or payload.body is not None or merge_md):
        # Backend-agnostic update (the old raw Postgres SQL — COALESCE/jsonb —
        # broke in SQLite/--lite mode).
        fields: dict = {}
        if payload.assignee_role:
            fields["assignee_role"] = _cfg.canonical_role(payload.assignee_role)
        if payload.labels is not None:
            fields["labels"] = payload.labels
        if payload.body is not None:
            fields["body"] = payload.body
        tickets_mod.patch_fields(t.id, fields=fields, metadata_patch=merge_md)
    return get_ticket(identifier)


@app.get("/api/workflows")
def list_workflows() -> list[dict]:
    """Public registry view — UI uses this to populate the workflow
    dropdown on the new-ticket form."""
    from aiforge_core.workflows import list_all
    return [w.to_public_dict() for w in list_all()]


# ─────────── Playbook Library: skills · workflows · rules ──────────────
# Operator-managed instruction library (separate from the ticket-routing
# /api/workflows registry above). Display all + create from text or via the
# configured LLM. These are the auto_context sources the agents pull from.

def _bundled_names(kind: str) -> set:
    """Filenames of the BUNDLED default playbooks for a kind. These ship in
    ``runtime/builtin_playbooks/{kind}`` and ``ensure_dirs()`` COPIES them into
    the user-writable global dir (keeping the filename), so a path check can't
    tell a seeded default from a user file — but the FILENAME still identifies
    it. Cached per process."""
    cache = _bundled_names.__dict__.setdefault("_cache", {})
    if kind not in cache:
        try:
            from pathlib import Path

            from aiforge_core.runtime import workflows as _wf
            d = Path(_wf.__file__).resolve().parent / "builtin_playbooks" / kind
            cache[kind] = {f.name for f in d.glob("*.md")} if d.is_dir() else set()
        except Exception:  # noqa: BLE001
            cache[kind] = set()
    return cache[kind]


def _library_origin(source: str, kind: str) -> str:
    """``"default"`` when the item is one of the bundled playbooks (matched by
    its source FILENAME), else ``"custom"`` — everything the user or a repo
    added. Never raises (classification must not break the listing)."""
    try:
        from pathlib import Path
        if source and Path(source).name in _bundled_names(kind):
            return "default"
    except Exception:  # noqa: BLE001
        return "custom"
    return "custom"


def _skill_dict(s, kind: str | None = None) -> dict:
    source = getattr(s, "source", "")
    return {"name": s.name, "description": s.description,
            "triggers": list(getattr(s, "triggers", []) or []),
            "body": s.body, "source": source,
            "always": bool(getattr(s, "always", False)),
            "origin": _library_origin(source, kind) if kind else "default"}


@app.get("/api/library/{kind}")
def library_list(kind: str) -> list[dict]:
    """List all skills / workflows / rules."""
    if kind == "skills":
        from aiforge_core.runtime import skills
        return [_skill_dict(s, "skills") for s in skills.load()]
    if kind == "workflows":
        from aiforge_core.runtime import workflows
        return [_skill_dict(w, "workflows") for w in workflows.load()]
    if kind == "rules":
        from aiforge_core.runtime import repo_rules
        return [{"name": r.name, "body": r.body, "source": r.source,
                 "globs": list(r.globs), "always": r.always,
                 "origin": _library_origin(r.source, "rules")}
                for r in repo_rules.load_global_rules()]
    raise HTTPException(404, f"unknown kind {kind!r}")


@app.post("/api/library/{kind}", status_code=201)
def library_create(kind: str, payload: dict = Body(...)) -> dict:
    """Create/overwrite a skill / workflow / rule from text."""
    name = (payload.get("name") or "").strip()
    body = (payload.get("body") or "").strip()
    desc = (payload.get("description") or "").strip()
    triggers = payload.get("triggers") or []
    if isinstance(triggers, str):
        triggers = [t.strip() for t in triggers.split(",") if t.strip()]
    if not name or not body:
        raise HTTPException(400, "name and body are required")
    if kind == "skills":
        from aiforge_core.runtime import skills
        res = skills.write_skill(name, desc, body, triggers)
    elif kind == "workflows":
        from aiforge_core.runtime import workflows
        res = workflows.write_workflow(name, desc, body, triggers)
    elif kind == "rules":
        from aiforge_core.runtime import repo_rules
        res = repo_rules.write_rule(name, body, globs=payload.get("globs"),
                                    always=bool(payload.get("always", True)))
    else:
        raise HTTPException(404, f"unknown kind {kind!r}")
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "write failed"))
    return res


_LIBRARY_GEN_PROMPT = {
    "skills": ("Write a SKILL.md. Output ONLY a markdown doc with YAML "
               "frontmatter (name, description, triggers: [..]) then a concise "
               "instruction body the agent follows. Topic: "),
    "workflows": ("Write a WORKFLOW.md: YAML frontmatter (name, description, "
                  "triggers: [..]) then numbered end-to-end steps. Topic: "),
    "rules": ("Write a coding RULE as a short markdown doc: one '# Title' then "
              "tight imperative bullet points the agent must follow. Topic: "),
}


@app.post("/api/library/{kind}/generate")
def library_generate(kind: str, payload: dict = Body(...)) -> dict:
    """Draft a skill / workflow / rule from a text description using the
    configured LLM. Returns the draft markdown for review before saving."""
    if kind not in _LIBRARY_GEN_PROMPT:
        raise HTTPException(404, f"unknown kind {kind!r}")
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    role = payload.get("role") or "architect"
    try:
        from aiforge_core.llm import client
        draft = client.complete(role, [
            {"role": "system", "content": "You author concise, high-signal "
             "agent instruction docs. Output ONLY the markdown, no preamble."},
            {"role": "user", "content": _LIBRARY_GEN_PROMPT[kind] + prompt},
        ], max_tokens=1200)
    except Exception as exc:  # noqa: BLE001 — surface model/credit errors
        raise HTTPException(502, f"LLM generate failed: {exc}")
    return {"ok": True, "draft": draft}


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


@app.post("/api/tickets/{identifier}/run-parallel", status_code=202)
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

    threading.Thread(target=_bg, name=f"parallel-{identifier}", daemon=True).start()
    return {"started": True, "identifier": identifier}


@app.post("/api/tickets/{identifier}/comments", status_code=201)
def add_comment(identifier: str, payload: CommentCreate) -> dict:
    t = tickets_mod.get(identifier)
    if t is None:
        raise HTTPException(404, f"ticket {identifier} not found")
    eid = tickets_mod.add_comment(t.id, payload.author, payload.body)
    return {"event_id": eid}


@app.post("/api/tickets/reset")
def tickets_reset() -> dict:
    """Delete ALL tickets + events and reset the ONE-<n> counter so the next
    ticket restarts the sequence. Worktrees / branches / PRs are NOT touched."""
    return {"ok": True, "deleted": tickets_mod.reset_all()}


@app.delete("/api/tickets/{identifier}", status_code=204)
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
import glob as _glob_mod  # local alias to avoid leaking name


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
    os.environ.get("AIFORGE_RUNTIME_ENV", os.path.join(
        os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge"), "runtime.env"))
)
_RUNTIME_ENV_LOCK = threading.Lock()


def _persist_env(key: str, value: str) -> None:
    """Upsert ``key=value`` into runtime.env so it survives a restart (the API
    reloads it with a plain KEY=VALUE parser at startup — see _load_runtime_env;
    it is NOT shell-sourced). Line-replace, order-preserving. Creates the file
    + dir when absent. Sanitises so the file stays a clean KEY=VALUE store:
    keys restricted to env-name chars; CR/LF stripped from the value (a newline
    could otherwise smuggle a second assignment into the file)."""
    key = re.sub(r"[^A-Za-z0-9_]", "", str(key))
    if not key:
        return
    value = str(value).replace("\r", " ").replace("\n", " ")
    with _RUNTIME_ENV_LOCK:                       # serialize concurrent PUTs
        try:
            os.makedirs(os.path.dirname(_RUNTIME_ENV_PATH), exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        lines: list[str] = []
        if os.path.isfile(_RUNTIME_ENV_PATH):
            with open(_RUNTIME_ENV_PATH) as _f:
                lines = _f.read().splitlines()
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


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


@app.get("/api/runtime/force_full_pipeline")
def get_force_full_pipeline() -> dict:
    """Whether the triage fast-path is disabled (every agent always runs)."""
    return {"enabled": _env_truthy("AIFORGE_FORCE_FULL_PIPELINE")}


@app.put("/api/runtime/force_full_pipeline")
def set_force_full_pipeline(payload: dict) -> dict:
    """Toggle running the FULL pipeline (skip the triage 'trivial' fast-path).
    Affects runs started after this call."""
    enabled = bool(payload.get("enabled"))
    val = "1" if enabled else "0"
    os.environ["AIFORGE_FORCE_FULL_PIPELINE"] = val
    try:
        _persist_env("AIFORGE_FORCE_FULL_PIPELINE", val)
    except Exception:  # noqa: BLE001
        pass
    return {"enabled": enabled, "persisted": True}


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
        # Don't echo the raw driver error — it can embed the bolt URI / creds.
        return {"backend": "neo4j", "total": 0, "wings": [],
                "error": type(exc).__name__}
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
        # Don't echo the raw driver error — it can embed the bolt URI / creds.
        return {"backend": "neo4j", "total": 0, "wings": [],
                "error": type(exc).__name__}
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


class _ValidatePathBody(BaseModel):
    location: str = Field(..., min_length=1, description="path to validate")


@app.post("/api/memory/validate-path")
def memory_validate_path(body: _ValidatePathBody) -> dict:
    """Pre-flight a repo/dir path BEFORE indexing — returns the resolved abs
    path + code/doc file counts so a wrong/empty/relative path is caught up
    front (the #1 cause of an index that silently produces 0 units)."""
    from aiforge_core.runtime.memory_ingest import validate_path
    return validate_path(body.location)


# ── Markdown-file memory (human-readable notes on disk + searchable) ──

@app.get("/api/memory/files")
def memory_files_list() -> list[dict]:
    from aiforge_core.memory import md_store
    return md_store.list_files()


@app.get("/api/memory/files/{name}")
def memory_files_get(name: str) -> dict:
    from aiforge_core.memory import md_store
    d = md_store.read_file(name)
    if d is None:
        raise HTTPException(404, f"no memory file: {name}")
    return d


class _MemFileBody(BaseModel):
    title: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    kind: str = Field("note")
    tags: list[str] | None = Field(None)


@app.post("/api/memory/files", status_code=201)
def memory_files_create(body: _MemFileBody) -> dict:
    from aiforge_core.memory import md_store
    return md_store.write(body.title, body.text, kind=body.kind,
                          tags=body.tags or [], source="manual")


@app.post("/api/memory/files/ingest")
def memory_files_ingest() -> dict:
    """(Re)ingest every md file in the memory dir into the search backend."""
    from aiforge_core.memory import md_store
    return md_store.ingest_dir()


@app.post("/api/memory/files/compact")
def memory_files_compact(group_by: str = Query("topic"),
                         min_group: int = Query(2, ge=2),
                         dry_run: bool = Query(False),
                         summarize: bool = Query(True),
                         model_role: str = Query("learner")) -> dict:
    """Consolidate per-session md memories into fewer standardized files.

    Group by ``topic`` (default — an LLM clusters notes into coherent topical
    files, so you get several browsable memories, not one blob per kind), or
    ``kind`` / ``tag`` / ``source``. With ``summarize``
    (default) an available LLM (``model_role``'s primary→cloud chain) rewrites
    each group into a compact, deduped document so the file stays small; falls
    back to a plain merge when no model is reachable. ``dry_run=true`` returns
    the plan without writing. Originals are archived (reversible)."""
    from aiforge_core.memory import md_store
    return md_store.compact(group_by=group_by, min_group=min_group,
                            model_role=model_role,
                            dry_run=dry_run, summarize=summarize)


@app.delete("/api/memory/files/{name}")
def memory_files_delete(name: str) -> dict:
    from aiforge_core.memory import md_store
    return {"deleted": md_store.delete_file(name), "name": name}


def _spawn_index(source_id: int) -> None:
    """Kick off ``run_index`` in a SEPARATE PROCESS, not a thread.

    Indexing is CPU-bound (tree-sitter parsing + chunking) and holds the GIL
    for long stretches; in an api thread it starves uvicorn's asyncio event
    loop and wedges every request — health, the UI, and the public tunnel all
    hang for the whole (minutes-long, CPU-embedding) index. A subprocess has
    its own GIL, so the api stays responsive. Detached + non-blocking; the
    child updates the source row's status itself."""
    import subprocess
    import sys
    try:
        subprocess.Popen(
            [sys.executable, "-m", "aiforge_core.runtime.memory_ingest",
             str(source_id)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001 — fall back to a thread
        import threading

        from aiforge_core.runtime.memory_ingest import run_index
        _af_log.warning("index subprocess spawn failed (%s); using a thread", exc)
        threading.Thread(target=run_index, args=(source_id,),
                         daemon=True).start()


@app.get("/api/memory/sources")
def memory_sources_list() -> list[dict]:
    from aiforge_core.runtime import memory_sources as _ms
    return _ms.list_sources()


@app.post("/api/memory/sources", status_code=201)
def memory_sources_create(body: _MemSourceBody) -> dict:
    """Register a memory source. ``repo``/``docs`` sources auto-start a full
    multi-layer background index immediately (chunks + tree-sitter symbols +
    graphify); ``url``/``file`` stay manual (cheap, index via /index)."""
    from aiforge_core.runtime import memory_sources as _ms
    try:
        src = _ms.create(body.kind, body.location, body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if body.kind in ("repo", "docs"):
        _ms.set_status(src["id"], "indexing", error=None)
        _spawn_index(src["id"])
        src = {**src, "status": "indexing"}
    return src


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
    from aiforge_core.runtime import memory_sources as _ms
    src = _ms.get(source_id)
    if not src:
        raise HTTPException(404, f"source {source_id} not found")
    _ms.set_status(source_id, "indexing", error=None)
    _spawn_index(source_id)
    return {**src, "status": "indexing"}


# ─────────────────── Memory admin (overview + clear) ───────────────────────
# High-level visibility into every memory datasource + a DESTRUCTIVE "empty
# this store" per datasource. These DELETE indexed DATA (graph nodes / SQLite
# units / on-disk notes / chat history) but NEVER the registered sources or
# config. They inherit the API-token auth (middleware); each also requires an
# explicit ``{"confirm": true}`` body so an accidental click can't wipe data.


class _MemConfirmBody(BaseModel):
    confirm: bool = Field(False, description="must be true to actually clear")


@app.get("/api/memory/overview")
def memory_overview_ep() -> dict:
    """Per-datasource breakdown: graph (facts/symbols/graphify/chunks), SQLite
    units, on-disk md notes, chat sessions, and registered sources. Each store
    soft-fails independently."""
    from aiforge_core.memory import admin as _admin
    return _admin.memory_overview()


@app.get("/api/memory/graph")
def memory_graph_ep(store: str,
                    limit: int = Query(60, le=300)) -> dict:
    """Small node-link sample of ONE graph store for an in-app SVG preview.
    ``store`` ∈ symbols | graphify | chunks | graph_facts. Soft-fails to
    ``{"available": False, "nodes": [], "edges": []}`` — never raises."""
    from aiforge_core.memory import admin as _admin
    return _admin.graph_sample(store, limit)


@app.get("/api/memory/graph/expand")
def memory_graph_expand_ep(store: str, node_id: str,
                           limit: int = Query(40, le=200)) -> dict:
    """Neighborhood of ONE node — the node + its directly-connected neighbors +
    connecting edges. ``store`` ∈ symbols | graphify | chunks | graph_facts.
    Soft-fails to ``{"available": False, "nodes": [], "edges": []}`` — never
    raises. Powers the in-app interactive graph explorer's click-to-expand."""
    from aiforge_core.memory import admin as _admin
    return _admin.graph_expand(store, node_id, limit)


@app.post("/api/memory/clear/{store}")
def memory_clear_store_ep(store: str,
                          body: "_MemConfirmBody | None" = None) -> dict:
    """Clear ALL data in ONE store. ``store`` ∈ graph_facts | symbols |
    graphify | chunks | sqlite | md_files | chat. Requires ``{confirm:true}``.
    Registered sources + configuration are preserved."""
    from aiforge_core.memory import admin as _admin
    if not (body and body.confirm):
        raise HTTPException(400, "confirm=true required to clear a memory store")
    try:
        return _admin.clear_store(store)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/memory/clear-all")
def memory_clear_all_ep(body: "_MemConfirmBody | None" = None) -> dict:
    """Wipe DATA across every memory store, preserving source registrations +
    config (their index state is reset to idle so they can be re-indexed).
    Requires ``{confirm:true}``."""
    from aiforge_core.memory import admin as _admin
    if not (body and body.confirm):
        raise HTTPException(400, "confirm=true required to wipe all memory")
    return _admin.clear_all()


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


_EXTRA_LOG_ROLES = {"intent", "publish", "integration", "adk_runner",
                    "enhancer", "architect", "verifier"}


@app.get("/api/logs/{role}/stream")
def stream_role_log(role: str):
    # Accept any role (sanitised) — an unknown role just tails an empty file
    # rather than 404-ing the tab. Prevents path traversal.
    role = re.sub(r"[^a-z0-9_]", "", (role or "").lower()) or "adk_runner"
    path = _resolve_role_log(role)

    async def gen():
        # Backfill the last ~200 lines on connect so the page shows recent
        # history immediately instead of a blank "waiting for events…".
        last_size = 0
        if os.path.exists(path):
            try:
                import collections as _coll
                # deque(maxlen) holds only the last 200 lines instead of
                # materialising the whole (append-only, unbounded) log file.
                with open(path, encoding="utf-8") as f:
                    tail = list(_coll.deque(f, maxlen=200))
                last_size = os.path.getsize(path)
                for line in tail:
                    line = line.strip()
                    if line:
                        yield f"data: {line}\n\n"
            except Exception:  # noqa: BLE001
                last_size = os.path.getsize(path) if os.path.exists(path) else 0
        try:
            while True:
                await asyncio.sleep(1.5)
                if not os.path.exists(path):
                    continue
                sz = os.path.getsize(path)
                if sz <= last_size:
                    continue
                with open(path, encoding="utf-8") as f:
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
                try:
                    proc.kill()
                    await proc.wait()   # reap — don't leak a zombie
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
            try:
                proc.kill()
                await proc.wait()   # reap — don't leak a zombie
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
        with open(err, encoding="utf-8", errors="replace") as f:
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
    insecure_tls: bool = Field(
        False, description="Skip TLS verification for this endpoint only "
                           "(self-signed / internal HTTPS box)")


class _ProviderTestBody(BaseModel):
    base_url: str | None = Field(
        None, description="OpenAI-compatible base URL to probe; falls back "
                          "to the saved base_url for `role` when omitted")
    api_key: str | None = Field(
        None, description="Bearer key; falls back to the saved token for "
                          "`role` when omitted (UI never echoes the secret)")
    insecure_tls: bool = Field(
        False, description="Skip TLS verification for this probe only")
    role: str | None = Field(
        None, description="Archetype whose saved creds fill blank fields, "
                          "so Test works after Save without re-typing the token")


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
            "insecure_tls": bool(row.get("insecure_tls")),
        }
    return out


@app.post("/api/providers/test")
def providers_test(body: _ProviderTestBody) -> dict:
    """Test-connection for the home page. Probes ``{base_url}/models`` and
    returns ``{ok, models[]}`` (or ``{ok:false, error}``).

    Blank ``base_url`` / ``api_key`` fall back to the saved config for
    ``role`` (resolved via env + stored row), so Test works right after
    Save — the UI never echoes the stored token back into the field, so
    without this fallback a post-Save Test would send no token and 401.
    """
    from aiforge_core.llm.providers.openai_compatible import probe
    base_url = (body.base_url or "").strip()
    api_key = (body.api_key or "").strip() or None
    insecure = bool(body.insecure_tls)
    if body.role and body.role in _acfg.archetypes():
        try:
            rl = _acfg.resolve_litellm(body.role)
        except Exception:
            rl = {}
        if not base_url:
            base_url = rl.get("api_base") or ""
        if not api_key:
            k = rl.get("api_key")
            api_key = None if (not k or k == "not-needed") else k
        insecure = insecure or bool(rl.get("insecure_tls"))
    logging.getLogger("aiforge.api").info(
        "POST /api/providers/test role=%s base_url=%s insecure_tls=%s token=%s",
        body.role, base_url, insecure, "yes" if api_key else "no")
    return probe(base_url, api_key, insecure=insecure)


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


# ── Model registry (simplified Settings: add models once, agents pick one) ────

class _ModelBody(BaseModel):
    label: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    insecure_tls: bool | None = None
    vision: str | None = Field(None, description="'auto' | 'yes' | 'no'")
    thinking: str | None = Field(None, description="reasoning model: 'auto' | 'yes' | 'no'")
    context_window: int | None = Field(None, ge=0, le=10_000_000,
                                       description="per-model input window (tokens); 0 = use global")


class _ApplyModelBody(BaseModel):
    roles: list[str] = Field(..., description="agent roles to point at this model")


@app.get("/api/agents/models")
def models_list() -> dict:
    from aiforge_core.config import model_registry
    return {"models": model_registry.list_models()}


def _reassign_by_capability() -> None:
    """Re-run capability-based agent auto-assignment. Called whenever the model
    set changes so the system always chooses each agent's model internally — no
    manual picking. Best-effort; never breaks the mutation that triggered it."""
    if os.environ.get("AIFORGE_AUTO_ASSIGN_AGENTS", "1") in ("0", "false"):
        return
    try:
        from aiforge_core.config import model_registry, agent_config
        model_registry.auto_assign(agent_config.archetypes())
    except Exception:  # noqa: BLE001
        pass


@app.post("/api/agents/models", status_code=201)
def models_add(body: _ModelBody) -> dict:
    from aiforge_core.config import model_registry
    if not (body.model or "").strip():
        raise HTTPException(400, "model id is required")
    try:
        row = model_registry.add_model(
            label=body.label or body.model, model=body.model,
            base_url=body.base_url or "", api_key=body.api_key,
            insecure_tls=(True if body.insecure_tls is None else bool(body.insecure_tls)),
            vision=body.vision or "auto", thinking=body.thinking or "auto",
            context_window=body.context_window or 0)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _reassign_by_capability()          # auto-decide agents on model add
    return row


@app.put("/api/agents/models/{model_id}")
def models_update(model_id: str, body: _ModelBody) -> dict:
    from aiforge_core.config import model_registry
    row = model_registry.update_model(
        model_id, label=body.label, model=body.model, base_url=body.base_url,
        api_key=body.api_key, insecure_tls=body.insecure_tls, vision=body.vision,
        thinking=body.thinking, context_window=body.context_window)
    if row is None:
        raise HTTPException(404, f"model {model_id} not found")
    # A vision change invalidates the probe cache for that model.
    try:
        from aiforge_core.runtime import chat_media
        chat_media.reset_vision_cache()
    except Exception:  # noqa: BLE001
        pass
    return row


@app.delete("/api/agents/models/{model_id}", status_code=204)
def models_delete(model_id: str) -> None:
    from aiforge_core.config import model_registry
    if not model_registry.remove_model(model_id):
        raise HTTPException(404, f"model {model_id} not found")
    _reassign_by_capability()          # re-decide agents after a model is removed


@app.post("/api/agents/models/sync")
def models_sync() -> dict:
    """Populate the registry from the agents' current per-role config (so it's
    not empty when models are already wired)."""
    from aiforge_core.config import model_registry
    res = model_registry.sync_from_config()
    _reassign_by_capability()          # auto-decide agents after sync
    return res


@app.post("/api/agents/models/{model_id}/apply")
def models_apply(model_id: str, body: _ApplyModelBody) -> dict:
    from aiforge_core.config import model_registry
    try:
        return model_registry.apply_to_roles(model_id, body.roles)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


class _AutoAssignBody(BaseModel):
    roles: list[str] | None = Field(None, description="roles to assign; default = all archetypes")
    dry_run: bool = Field(False, description="compute the plan without applying it")


@app.get("/api/agents/auto-assign")
def agents_auto_assign_preview() -> dict:
    """Preview capability-based assignments (thinking→reasoning model, coder→fast
    coder, vision→vision model) for every archetype — no changes applied."""
    from aiforge_core.config import model_registry, agent_config
    return {"assignments": model_registry.suggest_assignments(agent_config.archetypes())}


@app.post("/api/agents/auto-assign")
def agents_auto_assign(body: _AutoAssignBody) -> dict:
    """Auto-choose the best model for every agent BY CAPABILITY and apply it.
    Thinking/reasoning roles → a reasoning model, code roles → a fast coder,
    vision-needing → a vision model (larger context wins within a tier)."""
    from aiforge_core.config import model_registry, agent_config
    roles = body.roles or agent_config.archetypes()
    if body.dry_run:
        return {"assignments": model_registry.suggest_assignments(roles), "applied": False}
    out = model_registry.auto_assign(roles)
    out["applied"] = True
    return out


# ─────────────────────── MCP marketplace / installer ───────────────────────
class _McpInstallBody(BaseModel):
    catalog_id: str = Field(..., min_length=1)
    url: str | None = Field(None, description="override catalog url (required for custom)")
    name: str | None = Field(None, description="override display name")
    api_key: str | None = Field(None, description="optional bearer/api key")


class _McpUpdateBody(BaseModel):
    name: str | None = None
    url: str | None = None
    description: str | None = None
    enabled: bool | None = None
    api_key: str | None = None


@app.get("/api/mcp/catalog")
def mcp_catalog() -> dict:
    """The curated MCP marketplace catalog (browse → one-click install)."""
    from aiforge_core.config import mcp_registry
    return {"catalog": mcp_registry.load_catalog()}


@app.get("/api/mcp/servers")
def mcp_servers() -> dict:
    """Installed MCP servers (secrets stripped)."""
    from aiforge_core.config import mcp_registry
    return {"servers": mcp_registry.list_servers()}


@app.post("/api/mcp/servers", status_code=201)
def mcp_server_install(body: _McpInstallBody) -> dict:
    from aiforge_core.config import mcp_registry
    try:
        return mcp_registry.install_from_catalog(
            body.catalog_id, url=body.url, api_key=body.api_key, name=body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.put("/api/mcp/servers/{server_id}")
def mcp_server_update(server_id: str, body: _McpUpdateBody) -> dict:
    from aiforge_core.config import mcp_registry
    row = mcp_registry.update_server(
        server_id, name=body.name, url=body.url, description=body.description,
        enabled=body.enabled, api_key=body.api_key)
    if row is None:
        raise HTTPException(404, f"unknown MCP server: {server_id}")
    return row


@app.delete("/api/mcp/servers/{server_id}", status_code=204)
def mcp_server_delete(server_id: str) -> None:
    from aiforge_core.config import mcp_registry
    if not mcp_registry.remove_server(server_id):
        raise HTTPException(404, f"unknown MCP server: {server_id}")


@app.post("/api/mcp/servers/{server_id}/test")
def mcp_server_test(server_id: str) -> dict:
    """Connectivity check — list the server's tools via the MCP client."""
    from aiforge_core.config import mcp_registry
    from aiforge_core.runtime.tools import mcp_client
    row = mcp_registry.get_server(server_id)
    if row is None:
        raise HTTPException(404, f"unknown MCP server: {server_id}")
    name = row.get("name") or row.get("id")
    return mcp_client.list_tools(name)


@app.put("/api/agents/v2/{role}/config")
def agents_v2_set(role: str, body: _AgentConfigV2Body) -> dict:
    # "_default" is the global fallback every pipeline role inherits (the
    # home page's "Apply to all" writes it). Allowed alongside the named
    # archetypes so a single setting covers the ~16 internal roles.
    if role != _acfg._DEFAULT_KEY and role not in _acfg.archetypes():
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
                             api_key=api_key or None,
                             insecure_tls=body.insecure_tls)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "role": role,
        "provider": cfg.get("provider"),
        "model": cfg.get("model"),
        "base_url": cfg.get("base_url"),
        "api_key_set": bool(cfg.get("api_key")),
        "insecure_tls": bool(cfg.get("insecure_tls")),
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


@app.post("/api/agents/v2/reset")
def agents_v2_reset(keep_default: bool = Query(False)) -> dict:
    """Wipe the saved per-role agent config for a clean reconfigure.

    Removes stale per-role rows that can shadow a newly-set global default.
    ``keep_default=true`` preserves the global ``_default`` row and clears only
    the per-role overrides."""
    return _acfg.reset(keep_default=keep_default)


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
        "aiforge-graph-mcp",
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
def chat_retain(body: dict | None = None) -> dict:
    """Retention path was tied to the GA agent's auto-suggest. Now a
    no-op stub — explicit memory writes go through the new agent
    pipeline's Learner stage. (``_ChatRetainBody`` was deleted; the typed
    annotation became an undefined forward-ref that made FastAPI 422 the
    endpoint instead of returning the no-op — accept a plain body now.)"""
    return {"id": None, "retained": False, "reason": "deprecated"}


class _ChatMessage(BaseModel):
    role: str = Field("user", description="'user' or 'assistant'")
    content: str = Field("", description="message text")


class _ChatAgentBody(BaseModel):
    messages: list[_ChatMessage] = Field(..., description="conversation so far")
    cwd: str | None = Field(None, description="working directory; default workspace")
    role: str = Field("doer", description="archetype whose provider config drives the LLM")
    builder: str | None = Field(
        None, description="task charter: job|skill|workflow|rule (interactive "
        "builder that ends by calling the matching finalize tool)")


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
            for ev in run_chat_agent(msgs, cwd=cwd, role=body.role,
                                     builder=body.builder):
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


def _served_model_ids_for_role(role: str) -> set:
    """Served model IDs for a specific role's endpoint.

    openai_compatible has no static catalog — discover by probing the
    role's configured base_url (with its api_key + TLS settings) /models,
    exactly like the home-page Test. Falls back to provider-level
    discovery for local / ollama_cloud.
    """
    try:
        rl = _acfg.resolve_litellm(role)
    except Exception:
        rl = {}
    provider = (_acfg.get(role) or {}).get("provider") or "local"
    if provider == "openai_compatible":
        try:
            from aiforge_core.llm.providers.openai_compatible import probe
            res = probe(rl.get("api_base") or "", rl.get("api_key"),
                        insecure=bool(rl.get("insecure_tls")))
            return set(res.get("models") or [])
        except Exception:
            return set()
    return _served_model_ids(provider)


@app.get("/api/chat/models")
def chat_models() -> dict:
    """Models for the dedicated 'chat' slot. Lists only the provider's
    currently-served (active) models, flags whether the saved selection
    is still active so the UI can warn / re-pick."""
    row = _acfg.get("chat") if "chat" in _acfg.archetypes() else {}
    provider = row.get("provider") or "local"
    served = _served_model_ids_for_role("chat")
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
    apply_all: bool = Field(True, description="also set the global _default so "
                            "TEAM mode (all agents) uses this model")


@app.put("/api/chat/model")
def chat_model_set(body: _ChatModelBody) -> dict:
    """Persist the chat slot's model + report whether it's active (served
    right now). Rejected only on bad input — an inactive model is saved
    but flagged so the UI can warn."""
    cur = _acfg.get("chat") if "chat" in _acfg.archetypes() else {}
    provider = body.provider or cur.get("provider") or "local"
    try:
        # Preserve the chat endpoint's base_url / token / TLS opt-out — only
        # the model id is changing here. api_key=None is preserved by
        # set_role; insecure_tls must be passed through explicitly.
        cfg = _acfg.set_role("chat", provider, body.model,
                             base_url=cur.get("base_url"),
                             insecure_tls=bool(cur.get("insecure_tls")))
        # Apply to ALL agents by default: the picked model also becomes the
        # global _default so TEAM mode (triage/planner/doer/…) uses it too —
        # otherwise electing a bigger model only changes single-agent chat.
        if body.apply_all:
            gd = _acfg.get("_default") if "_default" in _acfg.archetypes() else {}
            _acfg.set_role("_default", provider, body.model,
                           base_url=cur.get("base_url") or gd.get("base_url"),
                           insecure_tls=bool(cur.get("insecure_tls")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    served = _served_model_ids_for_role("chat")
    return {"provider": cfg.get("provider"), "model": cfg.get("model"),
            "applied_to": "all agents" if body.apply_all else "chat only",
            "active": (cfg.get("model") in served) if served else True}


class _ModelReloadBody(BaseModel):
    model: str = Field(..., min_length=1)
    context_length: int = Field(..., ge=1024, le=2_097_152,
                                description="LM Studio --context-length; "
                                "clamped up to the 64K project floor")
    ttl: int = Field(0, ge=0, description="--ttl seconds; 0 = no idle unload")


@app.post("/api/chat/model/reload")
def chat_model_reload(body: _ModelReloadBody) -> dict:
    """(Re)load a model on the LM Studio host at a chosen context window.

    Powers the UI 'context window' control: SSHes to AIFORGE_LMS_HOST,
    unloads any running copy of the model, then ``lms load`` at the
    requested ctx. Blocking until the load returns. 503 when no LMS host
    is configured (e.g. a cloud-only deploy), 502 on SSH/load failure."""
    from aiforge_core.runtime import local_starter as _ls
    res = _ls.load_model_now(body.model, body.context_length, ttl=body.ttl)
    if not res.get("ok"):
        err = res.get("error", "reload failed")
        code = 503 if "AIFORGE_LMS_HOST" in err else 502
        raise HTTPException(code, err)
    return res


# Orchestrator = the 2 layer-1 agents (enhancer + planner) that analyze/enhance
# the request and split it into subtasks. Lets you run the splitter on a
# different (e.g. stronger reasoning) model than the workers.
_ORCHESTRATOR_ROLES = ("enhancer", "architect", "planner")


@app.get("/api/chat/orchestrator-model")
def orchestrator_model_get() -> dict:
    # The orchestrator picks from the SAME model universe as the worker/chat
    # slot — that's the real multi-model endpoint. Do NOT probe the planner
    # role: its base_url may be a per-model proxy (e.g. /proxy/<model>) that
    # serves one model and returns no /v1/models list, which would empty the
    # dropdown and spam "probe FAILED". Always include the current model so
    # the dropdown never renders empty.
    row = _acfg.get("planner") if "planner" in _acfg.archetypes() else {}
    served = set(_served_model_ids_for_role("chat"))
    current = row.get("model")
    if current:
        served.add(current)
    return {"provider": row.get("provider"), "model": current,
            "roles": list(_ORCHESTRATOR_ROLES),
            "models": [{"id": m, "label": m.split("/")[-1]} for m in sorted(served)]}


@app.put("/api/chat/orchestrator-model")
def orchestrator_model_set(body: _ChatModelBody) -> dict:
    """Set the model for the orchestrator's 2 agents (enhancer + planner)."""
    cur = _acfg.get("chat") if "chat" in _acfg.archetypes() else {}
    provider = body.provider or cur.get("provider") or "local"
    try:
        for role in _ORCHESTRATOR_ROLES:
            # Point at the CHAT slot's endpoint — the working multi-model
            # server. Reusing the role's own base_url would preserve a stale
            # per-model proxy (/proxy/<model>) and the picked model would 404.
            _acfg.set_role(role, provider, body.model,
                           base_url=cur.get("base_url"),
                           insecure_tls=bool(cur.get("insecure_tls")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "model": body.model, "roles": list(_ORCHESTRATOR_ROLES)}


# Global LLM token knobs — operator-chosen, no hardcoded constant wins over
# an explicit value. max_output_tokens = generation cap (file-write budget);
# context_window = assumed input window (escalation sizing).
class _RuntimeSettingsBody(BaseModel):
    max_output_tokens: int | None = Field(None, ge=256, le=1_000_000)
    context_window: int | None = Field(None, ge=1024, le=10_000_000)
    # 0/1 — force-treat the chat model as vision-capable (auto-detect still
    # applies when 0). Lets the user enable image Q&A for a self-hosted
    # multimodal model the allowlist doesn't recognise.
    vision_capable: int | None = Field(None, ge=0, le=1)
    # 0/1 — cave mode: send the agents the leanest useful context.
    cave_mode: int | None = Field(None, ge=0, le=1)
    # 0/1 — LLM-written, code-aware compaction (else cheap heuristic breadcrumb).
    compact_llm: int | None = Field(None, ge=0, le=1)
    # 0/1 disable flags for each dynamic-context block (default 0 = injected).
    ctx_no_recall: int | None = Field(None, ge=0, le=1)
    ctx_no_mentions: int | None = Field(None, ge=0, le=1)
    ctx_no_skills: int | None = Field(None, ge=0, le=1)
    ctx_no_workflows: int | None = Field(None, ge=0, le=1)
    ctx_no_repomap: int | None = Field(None, ge=0, le=1)
    ctx_no_summary: int | None = Field(None, ge=0, le=1)


@app.get("/api/runtime/llm-settings")
def llm_settings_get() -> dict:
    from aiforge_core.config import runtime_settings as _rs
    return _rs.all_settings()


@app.put("/api/runtime/llm-settings")
def llm_settings_set(body: _RuntimeSettingsBody) -> dict:
    from aiforge_core.config import runtime_settings as _rs
    vals = {k: v for k, v in body.model_dump().items() if v is not None}
    if not vals:
        raise HTTPException(400, "no settings provided")
    try:
        return _rs.set_many(vals)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class _RenameBody(BaseModel):
    title: str = Field(..., min_length=1)


class _SessionMsgBody(BaseModel):
    content: str = Field(..., min_length=1)
    role: str | None = Field(None, description="override the session's model (archetype)")
    mode: str = Field("simple", description="'simple' (single agent) | 'plan' (read-only single agent) | 'team' (full ADK flow)")
    review_edits: bool = Field(False, description="Hold every file-mutating tool call for human Approve/Reject (with diff) before it lands, in simple/plan mode. Default OFF — file writes/patches auto-apply. Opt in per-request here, or globally with AIFORGE_CHAT_REVIEW_EDITS=1.")
    edit_from_message_id: int | None = Field(None, description="Edit-and-resend: truncate history at this user message (restoring the workspace to that turn's checkpoint) before running this new content")
    builder: str | None = Field(None, description="task builder charter: job|skill|workflow|rule — runs an interactive single-agent builder that ends by calling the matching finalize tool (bypasses the enhancer/team pipeline)")


def _chat_workspace_root() -> str:
    return os.environ.get(
        "AIFORGE_CHAT_WORKSPACE_ROOT",
        os.path.join(os.path.expanduser(
            os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")), "chat-workspaces"))


def _delete_chat_workspace(cwd: str | None) -> bool:
    """``rm -rf`` a session's ISOLATED workspace when it is the managed,
    auto-created one under :func:`_chat_workspace_root`. Returns True if a dir
    was removed. Refuses anything else — a user-pinned project cwd, the root
    itself, or a path outside the managed tree — so clearing a chat can NEVER
    nuke a real repo. Leftover workspaces were the source of the "previous
    ticket's files leak into a new chat" bug; deleting them on clear removes it
    at the root (the per-turn baseline commit is the belt; this is the braces)."""
    if not cwd or not str(cwd).strip():
        return False
    import shutil
    try:
        root = os.path.realpath(_chat_workspace_root())
        target = os.path.realpath(str(cwd))
    except Exception:  # noqa: BLE001
        return False
    # Must be STRICTLY inside the managed root, and a session-* dir — never the
    # root itself, never a pinned repo, never a traversal escape.
    if target == root or not target.startswith(root + os.sep):
        return False
    if not os.path.basename(target).startswith("session-"):
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True


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


@app.post("/api/chat/sessions/reset")
def chat_sessions_reset() -> dict:
    """Delete ALL chat sessions + messages and reset the id sequence, AND rm -rf
    every managed session workspace so no stale files survive the clear."""
    from aiforge_core.runtime import chat_store
    # Snapshot each session's cwd before the rows go, so we delete exactly the
    # managed workspaces they owned (a pinned user repo is refused by the helper).
    cwds = [(s or {}).get("cwd") for s in (chat_store.list_sessions() or [])]
    deleted = chat_store.delete_all_sessions()
    removed = 0
    for _cwd in cwds:
        if _delete_chat_workspace(_cwd):
            removed += 1
    # Belt-and-braces: also sweep any orphaned session-* dirs left under the
    # managed root (e.g. from a session whose row was already gone).
    try:
        import shutil
        _root = _chat_workspace_root()
        for _name in os.listdir(_root):
            if _name.startswith("session-"):
                _p = os.path.join(_root, _name)
                if os.path.isdir(_p):
                    shutil.rmtree(_p, ignore_errors=True)
                    removed += 1
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "deleted": deleted, "workspaces_removed": removed}


@app.get("/api/chat/sessions/{session_id}")
def chat_session_get(session_id: int) -> dict:
    from aiforge_core.runtime import chat_store
    s = chat_store.get_session(session_id)
    if not s:
        raise HTTPException(404, f"session {session_id} not found")
    return {"session": s, "messages": chat_store.get_messages(session_id)}


@app.get("/api/chat/sessions/{session_id}/trace")
def chat_session_trace(session_id: int) -> dict:
    """Reviewable per-turn action+response trace (from ~/.aiforge/chat_traces).
    Each turn = {ts, mode, prompt, actions[], response, n_tools}."""
    from aiforge_core.runtime import chat_trace
    turns = chat_trace.read_turns(session_id)
    return {"session_id": session_id, "count": len(turns), "turns": turns}


@app.get("/api/chat/sessions/{session_id}/spec")
def chat_session_spec(session_id: int) -> dict:
    """The planner's SPEC.md (requirements + subtask breakdown) for this
    session's workspace — rendered as a markdown preview in the subtask dock."""
    from aiforge_core.runtime import chat_store
    sess = chat_store.get_session(session_id) or {}
    cwd = sess.get("cwd") or _default_cwd()
    path = os.path.join(cwd, "SPEC.md")
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                return {"exists": True, "path": path, "content": fh.read()[:200000]}
    except Exception as exc:  # noqa: BLE001
        return {"exists": False, "error": str(exc)}
    return {"exists": False, "content": ""}


@app.patch("/api/chat/sessions/{session_id}")
def chat_session_rename(session_id: int, body: _RenameBody) -> dict:
    from aiforge_core.runtime import chat_store
    s = chat_store.rename_session(session_id, body.title)
    if not s:
        raise HTTPException(404, f"session {session_id} not found")
    return s


@app.delete("/api/chat/sessions/{session_id}", status_code=204)
def chat_session_delete(session_id: int) -> None:
    from aiforge_core.runtime import (
        chat_approve, chat_cancel, chat_interject, chat_runs, chat_store,
    )
    # Stop any in-flight run first so its background producer doesn't keep
    # running + persisting against a session that no longer exists.
    chat_cancel.cancel(session_id)
    chat_approve.cancel(session_id)
    chat_interject.clear(session_id)
    chat_runs.finish(session_id)
    # Grab the isolated-workspace path BEFORE deleting the row so we can rm -rf
    # it — a lingering workspace's files otherwise leak into a future chat.
    _sess = chat_store.get_session(session_id)
    if not chat_store.delete_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    _delete_chat_workspace((_sess or {}).get("cwd"))


# ── Chat image attachments (upload + describe + query across the session) ─────

@app.post("/api/chat/sessions/{session_id}/media", status_code=201)
async def chat_media_upload(session_id: int, file: UploadFile = File(...)) -> dict:
    """Attach a file (image OR document — pdf/xlsx/docx/text) to a chat session:
    save it to the session's media folder, derive a description (vision caption
    for an image, extracted text for a document), and store the row. The
    description is what makes it queryable later in the session."""
    from aiforge_core.runtime import chat_media, chat_store
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    raw = await file.read()
    saved = chat_media.save_file(session_id, file.filename or "file", raw)
    if not saved.get("ok"):
        raise HTTPException(400, saved.get("error", "invalid file"))
    role = (chat_store.get_session(session_id) or {}).get("role") or "chat"
    try:
        # describe_upload runs a (slow) vision/text extraction — off the event
        # loop so one image upload doesn't block every other request.
        desc = await asyncio.to_thread(
            chat_media.describe_upload, saved["path"], saved["filename"],
            saved["mime"], role)
    except Exception:  # noqa: BLE001 — describe/extract is best-effort
        desc = ""
    row = chat_store.add_media(session_id, saved["filename"], saved["path"],
                               mime=saved["mime"], description=desc)
    row["kind"] = saved.get("kind")
    row["auto_described"] = bool(desc)
    return row


@app.get("/api/chat/sessions/{session_id}/media")
def chat_media_list(session_id: int) -> dict:
    from aiforge_core.runtime import chat_media, chat_store
    return {"media": chat_store.list_media(session_id),
            "vision": chat_media.vision_enabled(
                (chat_store.get_session(session_id) or {}).get("role") or "chat")}


class _MediaDescBody(BaseModel):
    description: str = Field("", description="user caption / edited description")


@app.patch("/api/chat/media/{media_id}")
def chat_media_describe(media_id: int, body: _MediaDescBody) -> dict:
    from aiforge_core.runtime import chat_store
    row = chat_store.set_media_description(media_id, body.description)
    if row is None:
        raise HTTPException(404, f"media {media_id} not found")
    return row


@app.delete("/api/chat/media/{media_id}", status_code=204)
def chat_media_delete(media_id: int) -> None:
    from aiforge_core.runtime import chat_store
    row = chat_store.delete_media(media_id)
    if row is None:
        raise HTTPException(404, f"media {media_id} not found")
    try:  # best-effort unlink the file
        if row.get("path") and os.path.isfile(row["path"]):
            os.remove(row["path"])
    except Exception:  # noqa: BLE001
        pass


@app.get("/api/chat/media/{media_id}/raw")
def chat_media_raw(media_id: int) -> FileResponse:
    from aiforge_core.runtime import chat_store
    row = chat_store.get_media(media_id)
    if row is None or not os.path.isfile(row.get("path") or ""):
        raise HTTPException(404, "media not found")
    return FileResponse(row["path"], media_type=row.get("mime") or "image/png")


def _step_digest(steps: list) -> str:
    """One compact line summarising what an assistant turn DID — tool calls +
    outcomes — so the next turn's history carries the agent's actions, not just
    its final prose. Fixes the 'forgets what it just did' amnesia: persisted
    `steps` were never fed back into context, so any work the model didn't
    transcribe into its final answer vanished."""
    if not isinstance(steps, list):
        return ""
    bits: list[str] = []
    for s in steps:
        if not isinstance(s, dict) or s.get("type") != "tool":
            continue
        name = s.get("name") or "tool"
        res = s.get("result") or {}
        # Tiny outcome marker: ok / err / a key field, kept short.
        mark = ""
        if isinstance(res, dict):
            if res.get("ok") is False or res.get("error"):
                mark = "✗"
            elif res.get("ok") is True:
                mark = "✓"
        arg = ""
        a = s.get("args") or {}
        if isinstance(a, dict):
            for k in ("path", "file", "cmd", "command", "query", "pattern"):
                if a.get(k):
                    arg = str(a[k])[:48]
                    break
        bits.append(f"{name}({arg}){mark}" if arg else f"{name}{mark}")
        if len(bits) >= 12:
            bits.append("…")
            break
    return ", ".join(bits)


def _chat_history_for_agent(rows: list) -> list[dict]:
    """Build the agent's conversation history from persisted messages.

    Unlike a naive role+content copy, this (1) folds each assistant turn's tool
    DIGEST into its content so the agent remembers its own prior actions, (2)
    keeps assistant turns that did work but produced no final text (don't drop
    them — that left a gap AND broke user/assistant alternation), and (3) merges
    consecutive same-role turns (some providers reject two in a row)."""
    out: list[dict] = []
    for m in rows:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        if role == "assistant":
            digest = _step_digest(m.get("steps") or [])
            if digest:
                content = (content + f"\n[did: {digest}]").strip() if content \
                    else f"[did: {digest}]"
        if not content:
            continue   # truly empty (e.g. a user turn with no text) — skip
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + content   # merge same-role
        else:
            out.append({"role": role, "content": content})
    return out


# Global cap on concurrent chat producer threads — a producer keeps running
# after the client disconnects (by design, for navigate-away survival), so
# without a cap N fired sessions = N background agent loops driving the model
# with nobody attached. Excess producers block at the start until a slot frees.
try:
    _PRODUCE_SEM = threading.BoundedSemaphore(
        max(1, int(os.environ.get("AIFORGE_MAX_CHAT_RUNS", "8"))))
except ValueError:
    _PRODUCE_SEM = threading.BoundedSemaphore(8)


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

    # Reject an overlapping run for the same session (two tabs, or a reattach
    # racing a send). Letting a 2nd producer start would replace this session's
    # cancel/approve token, so the 1st run's Stop becomes a no-op and BOTH
    # producers persist a turn (duplicate/garbled history). The client already
    # guards on `busy`; this is the server-side backstop. Use /attach to watch
    # the in-flight run instead.
    from aiforge_core.runtime import chat_runs
    if chat_runs.is_running(session_id):
        raise HTTPException(409, "a run is already in progress for this session "
                                 "— stop it or attach to it before sending again")

    role = body.role or session.get("role") or "chat"
    if body.role and body.role != session.get("role"):
        chat_store.set_session_role(session_id, body.role)

    # Edit-and-resend: when the client edits an earlier user turn, restore the
    # workspace to that turn's checkpoint (so the re-run starts from the same
    # state the original did) and truncate the conversation at that message
    # before appending the edited content. Best-effort restore — a missing
    # checkpoint just means history truncation without a workspace rollback.
    if body.edit_from_message_id:
        try:
            _cwd_er = session.get("cwd") or _default_cwd()
            _sha = chat_store.message_checkpoint(session_id, body.edit_from_message_id)
            if _sha:
                from aiforge_core.runtime import checkpoints as _ckpt
                _ckpt.restore(_cwd_er, _sha)
            chat_store.delete_messages_from(session_id, body.edit_from_message_id)
        except Exception as _exc:  # noqa: BLE001 — edit-resend must fail open
            _af_log.warning("edit-resend failed (session=%s msg=%s): %s",
                            session_id, body.edit_from_message_id, _exc)

    # Custom slash commands (Claude Code / Cursor parity, LOCAL files only).
    # A leading "/<name> args" whose <name> matches a user-defined command file
    # (.aiforge/commands/<name>.md or .claude/commands/<name>.md) expands to that
    # markdown template with $ARGUMENTS/$1.. substituted. Do it HERE — before the
    # message is persisted, titled, folded into `history`, or read as `prompt` —
    # so ONE interception covers simple, plan AND team modes (all of them derive
    # their prompt from body.content / the persisted history downstream). A
    # non-command message, a "/" typo, or an unknown /name expands to None and is
    # left verbatim. The built-in /help (and /commands) needs no user file and is
    # answered inline without invoking the model. Fail-open: any error → raw text.
    _cmd_expanded: str | None = None
    _cmd_help_text: str | None = None
    try:
        from aiforge_core.runtime import commands as _commands
        _cmd_cwd = session.get("cwd") or _default_cwd()
        _cmd_exp = _commands.expand(body.content, _cmd_cwd)
        if _cmd_exp is not None:
            _cmd_name = body.content.strip()[1:].split(None, 1)[0]
            _known = _cmd_name in _commands.load(_cmd_cwd)
            if not _known and _commands.is_builtin(_cmd_name):
                _cmd_help_text = _cmd_exp          # /help — answered inline
            else:
                body.content = _cmd_exp            # replace with expanded template
                _cmd_expanded = _cmd_name
    except Exception as _cexc:  # noqa: BLE001 — expansion must never break a turn
        _af_log.debug("slash-command expand skipped: %s", _cexc)

    _user_msg_id = chat_store.add_message(session_id, "user", body.content)
    # Provisional title now (instant), upgraded to a model-generated one after
    # the turn (see _produce). _fresh marks a still-unnamed session.
    _fresh_title = (session.get("title") or "New chat") == "New chat"
    if _fresh_title:
        # Clean deterministic provisional (strips 'Build a…', trailing clauses,
        # Title-Cases) — reads well instantly; upgraded by the model title below
        # when that succeeds. Beats the raw truncated first message.
        try:
            from aiforge_core.runtime import chat_title as _ct
            _prov = _ct.provisional_title(body.content) or body.content.strip()[:60]
        except Exception:  # noqa: BLE001
            _prov = body.content.strip()[:60]
        chat_store.rename_session(session_id, _prov)

    # Fold each assistant turn's tool digest into history + keep did-work-but-
    # blank turns + merge same-role runs, so the agent remembers what it DID
    # (not just what it said) on follow-ups.
    history = _chat_history_for_agent(chat_store.get_messages(session_id))
    cwd = session.get("cwd") or _default_cwd()
    team = body.mode == "team"
    from aiforge_core.runtime import parallel_subtasks as _psub
    agent_mode = "plan" if body.mode == "plan" else "act"
    prompt = body.content.strip()

    # Per-turn auto-route: once a team session has produced output, a small
    # follow-up ("rename that", "add a test") shouldn't re-run the whole heavy
    # pipeline (worktree + planner + verifier + slow Doer loop = minutes). A
    # cheap classify downgrades simple follow-ups to the fast single-agent
    # path. First team turn + genuinely complex follow-ups keep the pipeline.
    # Safe by default: any classifier failure leaves team=True. Disable with
    # AIFORGE_TEAM_AUTO_ROUTE=0.
    # NOTE: the actual classify call is deferred to the top of `_produce()`
    # (see below) — it's an LLM round-trip, and running it HERE, in the
    # synchronous request handler, delays the StreamingResponse from opening
    # at all: an unreachable/slow endpoint's retry+backoff chain (many
    # seconds) left the client with zero bytes and no ping, looking hung,
    # for a decision that only affects `_parallel_team` / `_events()` (both
    # only read once the background thread is already running).
    _auto_downgraded = False
    _parallel_team = False   # finalized in _produce(), once `team` is settled

    # Upgrade a freshly-named session to a concise MODEL-generated title,
    # CONCURRENTLY with the turn (a fast ~20-token call) so it neither blocks
    # the response nor lingers the stream. The client's post-turn session
    # refresh picks it up. Best-effort.
    if _fresh_title:
        def _gen_title():
            try:
                from aiforge_core.runtime import chat_store as _cs
                from aiforge_core.runtime import chat_title
                # Titling is a ~20-token throwaway — route it to the cheap
                # 'triage' role so it doesn't contend with the main turn on a
                # serial local endpoint (was the big session role).
                _t = chat_title.suggest_title(prompt, role="triage")
                if _t:
                    _cs.rename_session(session_id, _t)
            except Exception:  # noqa: BLE001 — titling must never break a run
                pass
        threading.Thread(target=_gen_title, daemon=True).start()

    from aiforge_core.runtime import chat_cancel
    chat_cancel.start(session_id)
    # Steering is accepted in every mode: simple/plan drain mid-run steers in the
    # ReAct loop; parallel folds them into SPEC.md (stream_parallel_team) to guide
    # the remaining subtasks + reconcile. (Sequential team clears them at end.)
    from aiforge_core.runtime import chat_interject as _chat_interject
    _chat_interject.set_steerable(session_id, True)
    # Gap D — arm/disarm the pre-apply review gate for this run. Cleared on
    # chat_approve.finish() in every termination path (simple/parallel here,
    # team in chat_pipeline), so it never leaks into the next turn. The
    # actual set_review_edits() call is deferred to the top of `_produce()`
    # (needs the post-classify `team` value — see the auto-route note above).
    from aiforge_core.runtime import chat_approve as _chat_approve

    def _auto_checkpoint():
        # Snapshot the working dir at turn start so the user can roll back
        # this turn's edits. Best-effort; gated by env. Runs INSIDE _gen
        # (first, before streaming) so its git subprocesses don't delay the
        # StreamingResponse from opening.
        if os.environ.get("AIFORGE_CHAT_AUTO_CHECKPOINT", "1") in ("0", "false") \
                or team:
            return
        try:
            import datetime as _dt

            from aiforge_core.runtime import checkpoints
            _snap = checkpoints.snapshot(
                cwd, label=f"before: {prompt[:50]}",
                when=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            # Stamp this turn's snapshot onto the user message so edit-resend
            # can restore the workspace to exactly this turn's starting state.
            if isinstance(_snap, dict) and _snap.get("ok") and _snap.get("sha"):
                chat_store.set_message_checkpoint(_user_msg_id, _snap["sha"])
        except Exception:  # noqa: BLE001
            pass

    # Records which path the run actually took, so the persistence gate below
    # matches. ``driver`` is True ONLY once the sequential team ADK driver
    # (chat_pipeline) has been launched — it self-persists and owns the run's
    # lifetime. Every other path (simple/plan, parallel, best-of-N, OR a team
    # run that crashes in the pre-stream orchestrator before the driver starts)
    # persists + cleans up inline here.
    _path = {"parallel": False, "driver": False}

    def _events():
        # Built-in /help (or /commands): answer inline with the command listing
        # and finish — no model call, works with zero user command files.
        if _cmd_help_text is not None:
            yield {"type": "message", "text": _cmd_help_text}
            yield {"type": "done"}
            return
        # Builder mode (job|skill|workflow|rule): a focused, deterministic
        # interactive builder. Bypass the enhancer/team/plan machinery (the
        # enhancer would distort the clarifying Q&A) and run the single chat
        # agent with the task charter, which ends by calling its finalize tool.
        if body.builder:
            # NOTE: do NOT re-import run_chat_agent here — a local import inside
            # this generator makes the name LOCAL to the whole generator, so the
            # non-builder paths below (which don't run this branch) hit it
            # unbound → "UnboundLocalError: run_chat_agent". Use the closure from
            # the outer function's import.
            from aiforge_core.runtime.prompts_extended import builders as _bld
            if _bld.charter_for(body.builder):
                yield from run_chat_agent(history, cwd=cwd, role=role,
                                          session_id=session_id, mode="act",
                                          builder=body.builder)
                return
        # A user command expanded — a small notice so the user sees WHY their
        # "/deploy …" turned into a longer prompt (the agent runs on the
        # expanded template below, unchanged).
        if _cmd_expanded:
            yield {"type": "thought", "role": "command",
                   "text": f"Expanded /{_cmd_expanded} command template."}
        # Rule / Memory / Feedback capture (deterministic, always-on) — runs
        # BEFORE any agent, independent of the agent's model, so a directive /
        # fact / correction stated in passing is captured + applied. FAILS OPEN:
        # any error here is swallowed and the normal run proceeds.
        try:
            from aiforge_core.runtime import rule_capture as _rc
            _repo = _rc.repo_key(cwd) or "repo"
            # PRE-FILTER: only spend an LLM classify when the message carries a
            # preference/directive cue. Ordinary turns ("hi", "fix the bug")
            # skip the classifier entirely — no per-turn LLM cost.
            if _rc.should_classify(prompt):
                import concurrent.futures as _cf

                def _capture_pass():
                    _c = _rc.classify(prompt, repo=_repo, session_id=session_id)
                    if _c.get("category") == "none":
                        return None
                    _s = _rc.store(_c, repo=_repo, session_id=session_id,
                                   repo_root=cwd)
                    # Recognition ONLY: detect a possible gate-disable request so
                    # the UI can OFFER an explicit opt-in. It sets NO flag.
                    _i = _rc.recognize_gate_intent(_c)
                    return _c, _s, _i

                # HARD wall-clock bound on the whole capture pass so a degraded
                # LLM can never stall the chat turn. Fully fail-open + fail-fast.
                _res = None
                _ex = _cf.ThreadPoolExecutor(max_workers=1)
                try:
                    _budget = float(os.environ.get("AIFORGE_CAPTURE_BUDGET_S", "6"))
                    _res = _ex.submit(_capture_pass).result(timeout=_budget)
                except Exception as _cexc:  # noqa: BLE001 — timeout/any → no capture
                    _af_log.debug("rule_capture pass timed out/failed: %s", _cexc)
                finally:
                    _ex.shutdown(wait=False)

                if _res is not None:
                    _cls, _stored, _intent = _res
                    _ev = {"type": "captured", "id": _stored.get("id"),
                           "category": _cls["category"], "scope": _cls["scope"],
                           "text": _cls.get("canonical", ""), "repo": _repo}
                    if _intent:
                        _ev["gate_intent"] = _intent     # UI offers opt-in pill
                    yield _ev
                    # PURE capture (no actionable task) → brief ack, skip the
                    # agent — UNLESS a deterministic actionable-intent backstop
                    # fires (e.g. "...and now fix the bug"): never drop a real
                    # task on the classifier's say-so.
                    if not _cls.get("task_present", True) \
                            and not _rc.looks_actionable(prompt):
                        yield {"type": "message",
                               "text": f"Got it — saved as {_cls['category']} "
                                       f"({_cls['scope']})."}
                        yield {"type": "done"}
                        return
        except Exception as _exc:  # noqa: BLE001 — capture must never break a turn
            _af_log.debug("rule_capture pre-agent pass failed: %s", _exc)
        # Team mode → full ADK agent flow (planner→…→learner) for complex
        # builds. Simple mode → single conversational agent for quick work.
        # Parallel team mode (AIFORGE_PARALLEL_SUBTASKS=1) → decompose then run
        # subtasks CONCURRENTLY in isolated worktrees with live status.
        from aiforge_core.runtime import parallel_subtasks as _pp
        # AUTO-ESCALATE: simple/plan modes on a multi-file BUILD request route
        # through the parallel pipeline — a single ReAct agent stalls on large
        # builds (one huge-context call, no decomposition). Gated + heuristic so
        # chit-chat / small edits still use the fast single-agent path.
        def _looks_like_multifile_build(p: str) -> bool:
            import re as _re
            p = (p or "").lower()
            if len(p) < 12:
                return False
            # DOCUMENT / non-code artifact tasks ("write a JIRA ticket for adding
            # rate limiting", "confluence page", "email", "spec doc") must NOT be
            # treated as a code build even though they mention build-y nouns.
            if _re.search(r"\b(jira|confluence|ticket|story|epic|description|"
                          r"document|doc|documentation|wiki|email|e-mail|report|"
                          r"summary|summari[sz]e|proposal|rfc|readme|changelog|"
                          r"release notes?|blog|article|memo|letter|announcement|"
                          r"agenda|minutes|slide|presentation|spec sheet)\b", p):
                return False
            verb = _re.search(r"\b(build|create|implement|generate|make|write|"
                              r"develop|code|scaffold)\b", p)
            noun = _re.search(r"\b(game|app|application|api|service|server|cli|"
                              r"tool|website|web ?app|webapp|system|library|"
                              r"package|project|backend|frontend|module|engine|"
                              r"bot|dashboard|parser|compiler|interpreter|crud|"
                              r"microservice|rest)\b", p)
            cues = any(c in p for c in ("with test", "unit test", "multiple file",
                                        " files", "test case", "endpoints"))
            return bool(verb and (noun or cues))

        def _is_advice_question(p: str) -> bool:
            """A QUESTION about how/whether to build — advice, not a build order.
            '_looks_like_analysis' misses these because they carry a build verb
            ('how do I BUILD a CLI?'); a fresh build must never fire on them."""
            import re as _re
            p = (p or "").strip().lower()
            if p.endswith("?"):
                return True
            return bool(_re.match(
                r"^(how|what|why|where|when|which|who|should|can|could|would|"
                r"is|are|do|does|did|explain|tell me|show me|help me understand|"
                r"any (idea|thought)|best way)\b", p))
        # NB: _parallel_team is `team and enabled()` — False for simple/plan. Use
        # the raw capability (_pp.enabled()) so escalation can fire off-team.
        _psub_on = False
        try:
            _psub_on = _pp.enabled()
        except Exception:  # noqa: BLE001
            _psub_on = _parallel_team
        _greenfield = True
        try:
            _greenfield = _pp._is_greenfield(cwd)
        except Exception:  # noqa: BLE001
            _greenfield = True
        # Escalate a MULTI-FILE BUILD to the pipeline — greenfield OR a genuinely
        # new subsystem on an existing repo (e.g. "build an auth module with
        # tests"). Safe on an existing repo: `_looks_like_multifile_build` matches
        # only build verbs (build/create/implement), NOT a "fix"/edit, and the
        # pipeline's greenfield-guard skips scaffold/off-plan-prune + never deletes
        # baseline files. A targeted edit still stays single-agent (no match).
        _build_escalate = bool(
            not team and _psub_on
            # PLAN mode is read-only — it must NEVER escalate into a pipeline that
            # writes files (that silently violated the plan contract).
            and agent_mode != "plan"
            # A question ("how do I build X with tests?") is advice, not a build
            # order — answer it, don't spin up the whole build pipeline.
            and not _is_advice_question(prompt)
            and os.environ.get("AIFORGE_AUTO_ESCALATE", "1") not in ("0", "false")
            and _looks_like_multifile_build(prompt))
        if _build_escalate:
            yield {"type": "thought", "role": "router",
                   "text": "Multi-file build detected — routing through the build "
                           "pipeline (decompose → scaffold → implement → test) "
                           "instead of a single agent."}
        # FOLLOW-UP / existing-repo routing: the decompose→build pipeline is for a
        # NEW project. A request on a repo that ALREADY has code — a follow-up
        # ("add a delete method", "fix the eviction bug") or an existing repo —
        # that isn't itself a fresh multi-file build is a TARGETED EDIT: route it
        # to the single agent (which sees the conversation history + the existing
        # files) instead of re-decomposing from scratch and clobbering prior work.
        _new_build = _looks_like_multifile_build(prompt)
        _route_pipeline = (_psub_on and (team or _build_escalate)
                           and (_greenfield or _new_build))
        if team and not _route_pipeline:
            yield {"type": "thought", "role": "router",
                   "text": "Existing code + a targeted change — editing in place "
                           "with the single agent (history + current files in "
                           "context), not a from-scratch rebuild."}
        # Review-edits is a simple/plan-only feature (forced on there). Team /
        # parallel / best-of-N runners run the full pipeline and don't hold
        # edits — left as-is by design, no notice (avoids per-run noise).
        if _route_pipeline:
            # Orchestrator (layer 1) = 3 agents: enhancer → architect → planner.
            # SCOPE the enhancer's memory recall to THIS session's repo — without
            # a repo, unified_query runs its repo-agnostic sources (prior chat
            # sessions + global vector) and an UNRELATED task bleeds into the
            # build spec (a "mathx" build decomposed into game/storage). The
            # contamination guard in unified_query only fires with a repo set.
            from aiforge_core.runtime.chat_agent import _chat_repo_key as _crk
            _pl_repo = _crk(cwd)
            _spec = _pp._enhance(prompt, history=history, cwd=cwd, repo=_pl_repo)  # 1. clean spec
            _files = _pp._architect(_spec, cwd=cwd)  # 2. design file structure
            _subs = _pp._plan_files(_files) if len(_files) >= 2 \
                else _pp._decompose(_spec)          # 3. split (per file, or plan)
            if len(_subs) >= 2:
                _path["parallel"] = True
                yield from _pp.stream_parallel_team(_spec, cwd=cwd, subtasks=_subs,
                                                    enhanced=True, session_id=session_id)
                # stream_parallel_team emits no terminal `done`; synthesize one
                # so a UI waiting on `done` doesn't hang (exactly one — the
                # exception path in _gen only fires on error).
                yield {"type": "done"}
                return
            # Couldn't split into ≥2 distinct files → it's really ONE task.
            # Best-of-N (Gap C, opt-in): when AIFORGE_BEST_OF_N is set, run the
            # single task N independent times in isolated worktrees, grade each,
            # keep the best. Otherwise fall back to the sequential team pipeline
            # so the user always gets a result. Default flow (flag unset) is
            # unchanged.
            if os.environ.get("AIFORGE_BEST_OF_N"):
                from aiforge_core.runtime import best_of_n as _bon
                _af_log.info("parallel decompose <2 subtasks — best-of-N route")
                _path["parallel"] = True
                yield from _bon.stream_best_of_n(_spec, cwd,
                                                 session_id=session_id)
                # stream_best_of_n emits no terminal `done`; synthesize one so a
                # UI waiting on `done` doesn't hang (exactly one).
                yield {"type": "done"}
                return
            _af_log.info("parallel decompose <2 subtasks — sequential fallback")
        if team:
            # Sequential team pipeline already has its own ADK enhancer agent;
            # don't double-enhance here. Mark the driver launched ONLY here —
            # so a crash in the parallel pre-steps above (which never reach this
            # line) still persists + cleans up inline in _gen's finally.
            _path["driver"] = True
            yield from stream_chat_pipeline(prompt, cwd=cwd, session_id=session_id,
                                            history=history)
            return
        # SIMPLE and PLAN modes — the Enhancer is MANDATORY on the FIRST turn
        # of a session (fresh context, referents to resolve, no memory pulled
        # yet). On a FOLLOW-UP, re-running the enhancer (an LLM round-trip
        # that also fires the memory recall inside `_enhance`) on every single
        # message is wasted latency for the common case ("fix that", "add a
        # test") — so reuse the same cheap classify already used to
        # auto-downgrade team turns (turn_router.classify) and skip the
        # enhancer when this follow-up is small. Any classify failure (or the
        # first turn, or a build-escalate spec already in flight) keeps the
        # enhancer mandatory — safe default, never silently under-enhance.
        _skip_enhance = _auto_downgraded and not _route_pipeline
        if not _skip_enhance and not _route_pipeline:
            try:
                from aiforge_core.runtime import turn_router as _tr2
                # Skip the enhancer (an LLM round-trip + a memory recall) on ANY
                # FOLLOW-UP that isn't itself a fresh multi-file build — the
                # history is folded into the spec regardless, so re-enhancing
                # "add rate limiting to the plan" / "fix that" is wasted latency
                # and a needless re-recall (was gated on classify=='simple',
                # which a plan-mode follow-up never matches, so it never skipped).
                # A genuinely NEW build follow-up still enhances.
                if _tr2.is_followup(history) \
                        and not _looks_like_multifile_build(prompt):
                    _skip_enhance = True
            except Exception as _sexc:  # noqa: BLE001 — never block a turn
                _af_log.debug("enhancer skip-check failed: %s", _sexc)
        if _auto_downgraded:
            yield {"type": "thought", "role": "router",
                   "text": "Small follow-up — handling directly (skipped the "
                           "full pipeline for speed)."}
        if _skip_enhance:
            _enriched = prompt
        else:
            yield {"type": "thought", "role": "enhancer",
                   "text": "Enhancing request + gathering context…"}
            # Fold `history` INTO the spec (restores referent resolution: a
            # context-dependent follow-up like "no, use postgres instead" or
            # "fix that bug" must be resolved against the prior turns, else
            # the enhancer fabricates a context-free spec that REPLACES the
            # user's words).
            from aiforge_core.runtime.chat_agent import _chat_repo_key as _crk2
            _enriched = _pp._enhance(prompt, history=history, cwd=cwd,
                                     repo=_crk2(cwd))   # scope recall (anti-contamination)
        # Replace the LAST user turn's content with the enriched spec, keeping
        # every prior turn intact. Trimming the recent turns (an earlier "avoid
        # the double-fold" attempt) broke claude_local's user/assistant
        # alternation and dropped context when `_enhance` no-ops on a trivial
        # follow-up ("yes"/"no"). The residual double-fold (recent turns appear
        # raw AND folded into the spec) is benign token redundancy, not semantic
        # harm; alternation stays intact and no turn is ever dropped.
        _enriched_history = [dict(m) for m in history]
        for _m in reversed(_enriched_history):
            if _m.get("role") == "user":
                # AUGMENT, don't replace: keep the user's verbatim words and
                # attach the enhancer's interpretation as a clearly-labelled
                # block the model can cross-check. A distorted/hallucinated
                # enhancement no longer silently becomes the request (the raw
                # ask is right there). If _enhance no-ops, skip the block.
                _raw = (_m.get("content") or "").strip()
                if _enriched and _enriched.strip() and _enriched.strip() != _raw:
                    _m["content"] = (
                        f"{_raw}\n\n---\n[Interpreted request — a context-enriched "
                        f"restatement; if it conflicts with my words above, my "
                        f"words win:]\n{_enriched}")
                break
        if agent_mode == "plan":
            _subs = _pp._decompose(_enriched)       # Planner
            if _subs:
                # Plan mode shows a STATIC plan it never executes — mark them
                # "planned" (not "pending") so the UI doesn't render them as
                # stuck-forever pending-execution rows.
                yield {"type": "subtasks", "items": [
                    {"slug": s.get("slug") or f"sub-{i+1}",
                     "goal": s.get("goal") or s.get("title") or "",
                     "status": "planned"}
                    for i, s in enumerate(_subs)]}
            # Plan→approve→execute (Gap B): hand the approved spec to the UI so
            # the user can one-click "Approve & Execute" — which re-sends this
            # enriched spec as a TEAM run. Persisted so the button survives a
            # reload until the plan is acted on. Emit plan_ready BEFORE the
            # agent's terminal `done` reaches the client (hold the `done`, yield
            # plan_ready, then release `done`) so the UI sees the plan, not a
            # finished turn with no plan.
            _pending_done = None
            for _ev in run_chat_agent(_enriched_history, cwd=cwd, role=role,
                                      session_id=session_id, mode="plan"):
                if _ev.get("type") == "done":
                    _pending_done = _ev
                    continue
                yield _ev
            yield {"type": "plan_ready", "spec": _enriched}
            if _pending_done is not None:
                yield _pending_done
            return
        # Baseline commit so we can show a Changes diff after the single-agent run
        # (simple mode edits the working tree; the pipeline shows its own Changes).
        # A fresh chat workspace is NOT a git repo — the old rev-parse/empty-tree
        # dance then left _simple_sha unusable (git diff needs a real repo), so the
        # Changes view silently vanished. _ensure_git_workspace git-inits + makes a
        # committed baseline (no-op when cwd is already a repo, e.g. a pinned user
        # project), so HEAD is ALWAYS a valid baseline to diff the run against.
        # CRITICAL: commit the CURRENT working-tree state into the baseline so
        # this turn's Changes diff + the "did it write source?" gate reflect ONLY
        # what THIS turn does. A reused chat/ticket workspace (e.g. session-1)
        # carries a previous task's uncommitted files; without this snapshot,
        # `git status` reports THEM, so a no-code Jira/Q&A turn wrongly triggers
        # the build/integration pipeline on stale files and the Changes view
        # shows the previous ticket's edits.
        _simple_sha = ""
        try:
            from aiforge_core.runtime.parallel_subtasks import _commit_turn_baseline
            _simple_sha = _commit_turn_baseline(cwd)
        except Exception:  # noqa: BLE001
            _simple_sha = ""
        yield from run_chat_agent(_enriched_history, cwd=cwd, role=role,
                                  session_id=session_id, mode=agent_mode)
        # Read-only / analysis query ("analyze/explain/how does X work") → the user
        # wants an EXPLANATION, not a build. Don't run the integration-check +
        # self-heal (which would build/test an existing repo and report a failure
        # instead of the analysis).
        def _looks_like_analysis(p: str) -> bool:
            import re as _re
            p = (p or "").lower()
            ask = _re.search(r"\b(analy[sz]e|explain|describe|summar[iy][sz]e|"
                             r"review|understand|audit|document|investigate|trace|"
                             r"walk\s*(me)?\s*through|how\s+(does|do|is|are)|"
                             r"what\s+(does|is|are)|why\s+(does|is|are)|where\s+"
                             r"(is|are)|tell me about|show me how)\b", p)
            change = _re.search(r"\b(fix|create|build|implement|add|write|refactor|"
                                r"rename|delete|remove|update|generat|make|"
                                r"modify|patch|scaffold)\b", p)
            return bool(ask and not change)

        _readonly = _looks_like_analysis(prompt)

        def _wrote_source() -> bool:
            """True only if this turn CREATED/MODIFIED a source file — the signal
            there's something to build+test. A JIRA/Confluence/Q&A/chat/analysis
            turn touches no source, so the integration-check (+ its hardcoded
            python fallback steps) must NOT run for it."""
            exts = (".py", ".java", ".go", ".js", ".mjs", ".ts", ".tsx", ".c",
                    ".cc", ".cpp", ".h", ".hpp", ".rs", ".rb", ".php", ".cs",
                    ".kt", ".swift", ".scala", ".sh")
            try:
                import subprocess as _sp
                _r = _sp.run(["git", "-C", cwd, "status", "--porcelain"],
                             capture_output=True, text=True, timeout=10)
                if _r.returncode == 0:
                    # git ran cleanly → the working tree IS the answer. Because a
                    # pre-turn baseline commit was taken, this reflects ONLY this
                    # turn's writes: source touched → True, otherwise (empty tree
                    # OR non-source changes) → False. Do NOT fall through to the
                    # process-global touched_paths(), which can hold a PRIOR turn's
                    # path and would re-trigger the build on a no-code turn.
                    return any(ln[3:].strip().endswith(exts)
                               for ln in (_r.stdout or "").splitlines() if ln.strip())
            except Exception:  # noqa: BLE001 — git missing / timeout
                pass
            # Fallback ONLY when git is unusable (not a repo): best-effort.
            try:
                from aiforge_core.runtime.doer_tools import touched_paths
                return any(str(p).endswith(exts) for p in touched_paths())
            except Exception:  # noqa: BLE001
                return False

        # Simple/act mode: after the agent finishes, COMPILE + run the project's
        # tests and report — ONLY when this turn actually wrote source code. Skips
        # plan mode, read-only analysis, and every non-code task (JIRA/Confluence/
        # Q&A/chat). Best-effort; env-gated off with AIFORGE_CHAT_INTEGRATION_TEST=0.
        # PROPORTIONALITY: only run the (heavy) build+test+self-heal when there's
        # something to verify — a detectable build/test stack. A doc/config/tiny
        # edit in a repo with no tests + no build system gets the Changes diff, not
        # a pointless build cycle. AIFORGE_CHAT_INTEGRATION_TEST=0 disables entirely.
        def _worth_verifying() -> bool:
            try:
                from aiforge_core.runtime.tools.project_runner import (
                    _has_tests, detect,
                )
                stacks = (detect(cwd) or {}).get("stacks") or []
                if stacks and _has_tests(cwd, stacks):
                    return True
                # bare python (no marker) but with test files → still worth it
                import glob
                return bool(glob.glob(os.path.join(cwd, "**", "test_*.py"),
                                      recursive=True)
                            or glob.glob(os.path.join(cwd, "**", "*_test.py"),
                                         recursive=True))
            except Exception:  # noqa: BLE001
                return True   # unsure → keep the old behaviour (verify)
        if agent_mode != "plan" and not _readonly and _wrote_source() \
                and os.environ.get(
                    "AIFORGE_CHAT_INTEGRATION_TEST", "1") not in ("0", "false") \
                and _worth_verifying():
            try:
                from aiforge_core.runtime.parallel_subtasks import (
                    _reconcile_integration,
                )
                yield {"type": "thought", "role": "verifier",
                       "text": "Building + running integration tests…"}
                # Same self-heal as the pipeline: build+test, and if it fails,
                # rewrite the offending files until green (bounded), then report.
                _ires: dict = {}
                yield from _reconcile_integration(cwd, _ires)
                _rep = _ires.get("rep") or {}
                # Only show the integration report when a build/test ACTUALLY ran
                # (ok True/False). ok=None = "no build markers / no toolchain" — a
                # simple file edit with no tests — so DON'T dump the "build & test
                # it yourself (python)" boilerplate as the answer; the Changes diff
                # below is the useful output.
                if _rep.get("md") and _rep.get("ok") is not None:
                    # supplementary=True: render the build report but DON'T let it
                    # replace the agent's own answer as the persisted final_text.
                    yield {"type": "message", "text": _rep["md"],
                           "role": "verifier", "supplementary": True}
            except Exception as _iexc:  # noqa: BLE001 — never break the turn
                _af_log.debug("integration report skipped: %s", _iexc)
        # SHOW CHANGES (simple mode too) — a clean PR-style diff of what the single
        # agent edited, same view as the pipeline. Working-tree diff (uncommitted).
        # Gated on `not _readonly` ONLY (NOT _wrote_source, which lists code
        # extensions) so a doc/config-only edit (README, yaml, json, Dockerfile)
        # still shows its diff. _emit_changes self-guards on an empty diff, so a
        # pure Q&A turn that wrote nothing simply emits no changes event.
        if _simple_sha and not _readonly:
            try:
                from aiforge_core.runtime.parallel_subtasks import _emit_changes
                yield from _emit_changes(cwd, _simple_sha, include_worktree=True)
            except Exception as _cx:  # noqa: BLE001
                _af_log.debug("simple changes diff skipped: %s", _cx)

    # The PRODUCER runs on a background daemon thread and publishes every event
    # into the per-session run registry (chat_runs). It NO LONGER yields to the
    # HTTP response, so a client that navigates away (aborting the fetch) can't
    # kill the run — the thread runs to completion and persists the full turn.
    # The HTTP response (and any later /attach) just SUBSCRIBES and tails the
    # buffer. This is the same survive-the-disconnect pattern team mode already
    # used internally, now applied to every mode. (chat_runs imported above for
    # the is_running concurrency guard.)
    run = chat_runs.start(session_id)

    def _produce():
        nonlocal team, _auto_downgraded, _parallel_team
        _PRODUCE_SEM.acquire()   # bounded — block until a producer slot frees
        # Auto-route classify + its dependents, run HERE (already off the
        # response-open path — see the note where `team`/`_parallel_team`
        # were declared above) rather than in the synchronous request
        # handler, so a slow/unreachable classify LLM never delays the
        # StreamingResponse itself.
        if team:
            try:
                from aiforge_core.runtime import turn_router as _tr
                if _tr.should_downgrade_team(prompt, history, cwd):
                    team = False
                    _auto_downgraded = True
                    _af_log.info("chat: team turn auto-downgraded to simple "
                                 "(small follow-up) session=%s", session_id)
            except Exception as _rexc:  # noqa: BLE001 — routing must never block a turn
                _af_log.debug("turn_router skipped: %s", _rexc)
        _parallel_team = team and _psub.enabled()
        # Review-edits gate: OFF by default — file writes/patches auto-apply,
        # no per-edit Approve/Reject prompt (the operator asked for no file-
        # permission prompts). Re-enable per-request via body.review_edits, or
        # globally with AIFORGE_CHAT_REVIEW_EDITS=1. Team/parallel mode never
        # holds edits regardless (the full pipeline runs unattended).
        _review_env = os.environ.get(
            "AIFORGE_CHAT_REVIEW_EDITS", "0") in ("1", "true", "yes", "on")
        _chat_approve.set_review_edits(
            session_id, (bool(body.review_edits) or _review_env) and not team)
        steps: list[dict] = []
        final_text = ""
        awaiting = False   # turn ended with a question / pause, not an outcome
        _subtasks: list[dict] = []   # live subtask panel state, persisted so it
        #                              survives a navigate-away / reload
        # Mirror chat activity into the observability NDJSON so the Logs page
        # shows live runs (the page tails orchestrator-<role>.ndjson).
        try:
            from aiforge_core.observability.logging import emit, get_logger
            # ONE shared "chat" logger (so the Logs "chat" tab tails one file).
            # Don't stash a per-session ticket on the process-wide singleton —
            # concurrent sessions would clobber it; stamp `session` per emit below.
            _clog = get_logger("chat")
        except Exception:  # noqa: BLE001
            _clog = None
            emit = None  # type: ignore
        _auto_checkpoint()   # snapshot first (off the response-open path)
        # Terminal subtask statuses — a cancelled run coerces any non-terminal
        # row to "failed" so the persisted/reloaded panel never shows a row
        # stuck pending/running after a Stop.
        # "planned" is a settled, never-executed plan-mode state — NOT in-flight,
        # so a cancel must not flip it to "failed".
        _TERMINAL = {"done", "failed", "skipped", "won", "planned"}
        emitted_done = False   # forwarded a terminal `done` yet?
        try:
            for ev in _events():
                if _clog is not None and emit is not None and \
                        ev.get("type") in ("thought", "tool", "message", "error"):
                    try:
                        emit(_clog, ev["type"], session=session_id, name=ev.get("name"),
                             text=(ev.get("text") or "")[:200],
                             tool_ok=(ev.get("result") or {}).get("ok") if isinstance(ev.get("result"), dict) else None)
                    except Exception:  # noqa: BLE001
                        pass
                if ev.get("type") == "message" and not ev.get("supplementary"):
                    # A supplementary message (e.g. the build/integration report)
                    # renders but must NOT replace the agent's own answer as the
                    # persisted final_text — persist it as a step instead.
                    final_text = ev.get("text", "")
                    awaiting = bool(ev.get("awaiting_input"))
                elif ev.get("type") == "message" and ev.get("supplementary"):
                    steps.append(ev)
                elif ev.get("type") in ("thought", "tool", "error", "changes"):
                    steps.append(ev)
                elif ev.get("type") == "subtasks":
                    _subtasks = list(ev.get("items") or [])
                elif ev.get("type") == "subtask_update":
                    for _s in _subtasks:
                        if _s.get("slug") == ev.get("slug"):
                            _s["status"] = ev.get("status")
                elif ev.get("type") == "plan_ready":
                    # Persist the approvable plan (Gap B) so the "Approve &
                    # Execute" button survives a reload.
                    steps.append(ev)
                elif ev.get("type") == "captured":
                    # Persist the capture pill so the inline "Saved RULE · scope"
                    # note (change-scope / undo) survives a reload.
                    steps.append(ev)
                if ev.get("type") == "done":
                    emitted_done = True
                run.publish(ev)
                if chat_cancel.is_cancelled(session_id):
                    # Stop pressed mid-stream (parallel / best-of-N break out
                    # BEFORE their synthesized `done`): reconcile any in-flight
                    # subtask row to a terminal state so nothing reloads stuck.
                    for _s in _subtasks:
                        if _s.get("status") not in _TERMINAL:
                            _s["status"] = "failed"
                    break
            # Persist the final subtask panel as a step so reload restores it.
            if _subtasks:
                steps.insert(0, {"type": "subtasks", "items": _subtasks})
            # The UI unblocks on a terminal `done`. A cancelled parallel/
            # best-of-N run breaks before its synthesized `done`, so guarantee
            # exactly one here when none was forwarded (non-cancel paths already
            # emit their own — don't double-emit).
            if not emitted_done:
                run.publish({"type": "done"})
                emitted_done = True
        except Exception as exc:  # noqa: BLE001
            run.publish({"type": "error", "text": str(exc)})
            run.publish({"type": "done"})
        finally:
            # Capture cancellation BEFORE finishing the token (finish pops
            # it, after which is_cancelled always reads False).
            cancelled = chat_cancel.is_cancelled(session_id)
            # TEAM mode: the background driver owns the run's lifetime AND its
            # persistence (chat_pipeline._drive) — it survives a client
            # disconnect and holds the real final answer, so we must NOT
            # persist a partial here (and finishing the token here would
            # orphan a still-running ADK run on Stop). SIMPLE mode runs inline
            # in this producer thread, so finish + persist here.
            # Parallel team mode is a self-contained generator (not the
            # background ADK driver), so persist it inline like simple mode.
            # The sequential fallback uses the team driver, which self-persists.
            # Gate on whether that driver actually LAUNCHED — a team run that
            # crashes in the pre-stream orchestrator (enhance/architect/
            # decompose) never starts the driver, so it must clean up here too.
            if not _path["driver"]:
                chat_cancel.finish(session_id)
                from aiforge_core.runtime import chat_interject
                chat_interject.clear(session_id)   # no stale steers next turn
                from aiforge_core.runtime import chat_approve, chat_persist
                chat_approve.finish(session_id)
                chat_persist.persist_turn(
                    session_id=session_id, cwd=cwd, prompt=prompt,
                    final_text=final_text, steps=steps,
                    team=(team or _path["parallel"]),
                    cancelled=cancelled, awaiting=awaiting)
                # Single-chat (simple/plan) memory writeback. The team
                # pipeline runs a Learner node + memory callbacks itself;
                # the inline simple/plan path never did, so chat work never
                # reached long-term memory. Distil + persist durable facts
                # on a daemon thread (off the response path). Skip cancelled
                # turns and the parallel-team path (its own runners cover it).
                if not cancelled and not team and not _path["parallel"]:
                    def _chat_learn():
                        try:
                            from aiforge_core.runtime import chat_learner
                            from aiforge_core.runtime.chat_agent import _chat_repo_key
                            # Same key resolution as RECALL (_chat_repo_key,
                            # git-toplevel basename) — the old bare repo_key(cwd)
                            # filed subdir-pinned sessions under the subdir while
                            # recall read the repo root, so facts were never found.
                            _repo = _chat_repo_key(cwd)
                            chat_learner.learn_from_chat(
                                prompt=prompt, final_text=final_text,
                                steps=steps, repo=_repo, session_id=session_id)
                        except Exception:  # noqa: BLE001
                            pass
                    threading.Thread(target=_chat_learn, daemon=True).start()
                    # Boundary-gated per-SESSION summary → browsable md file +
                    # memory graph (Neo4j when configured). Refreshes an
                    # upsert'd summary every N turns as the session grows (one
                    # cheap-tier LLM call, capped) so cross-session recall goes
                    # through unified_query's graph instead of a substring scan.
                    # Best-effort on a daemon thread — a failure here must never
                    # affect the turn.
                    def _chat_summarize():
                        try:
                            from aiforge_core.runtime import chat_store, chat_summary
                            from aiforge_core.runtime.chat_agent import _chat_repo_key
                            every = 4
                            try:
                                every = max(1, int(os.environ.get(
                                    "AIFORGE_CHAT_SUMMARY_EVERY", "4")))
                            except (TypeError, ValueError):
                                every = 4
                            n = len(chat_store.get_messages(session_id))
                            if n <= 0 or n % every != 0:
                                return
                            _repo = _chat_repo_key(cwd)   # git-toplevel, matches recall
                            chat_summary.summarize_session(session_id, _repo)
                        except Exception:  # noqa: BLE001
                            pass
                    threading.Thread(target=_chat_summarize, daemon=True).start()
            # Wake every subscriber (this stream + any /attach) and close THIS
            # run object (not by session id — a newer turn for the same session
            # may have already replaced it in the registry). Done LAST so a
            # re-attach during persistence still tails live.
            run.finish()
            try:
                _PRODUCE_SEM.release()
            except (ValueError, RuntimeError):   # never over-release
                pass

    threading.Thread(target=_produce, daemon=True).start()

    def _stream():
        # Tail the live run as SSE. A client disconnect only closes this
        # subscriber — the producer thread keeps running.
        q = run.subscribe()
        for ev in chat_runs.iter_subscription(run, q):
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


@app.get("/api/chat/sessions/{session_id}/attach")
def chat_session_attach(session_id: int) -> StreamingResponse:
    """Re-attach to an in-flight run after navigating back to the Chat view.

    Replays the run's buffered events (so the client rebuilds the live turn
    from the start — thoughts, tools, subtasks, the in-progress answer) and
    then tails live events to completion. If no run is in flight for this
    session, emits a single ``done`` immediately so the client knows there's
    nothing live to resume (and can just show the persisted history)."""
    from aiforge_core.runtime import chat_runs

    def _gen():
        # First event always tells the client whether there's a live run, so it
        # can decide to show progress (running) or just keep the persisted
        # history (not running) — no guessing from the event stream.
        run = chat_runs.get(session_id)
        running = bool(run and not run.done)
        _att = {"type": "attached", "running": running}
        if running and run is not None:
            _att["started_at"] = run.started_at   # epoch secs → true elapsed
        yield f"data: {json.dumps(_att)}\n\n"
        if not running or run is None:
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        q = run.subscribe()
        for ev in chat_runs.iter_subscription(run, q):
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")


@app.post("/api/chat/sessions/{session_id}/stop")
def chat_session_stop(session_id: int) -> dict:
    """Stop the in-flight chat run for this session — signals the agent
    loop / ADK pipeline to halt and kills any subprocess groups it
    spawned (builds, test runs). Idempotent."""
    from aiforge_core.runtime import chat_approve, chat_cancel
    active = chat_cancel.cancel(session_id)
    chat_approve.cancel(session_id)   # unblock any pending approval gate
    return {"stopped": active, "session_id": session_id}


@app.post("/api/chat/kill-all")
def chat_kill_all() -> dict:
    """Force-reset ALL in-flight chat state — the 'kill all' escape hatch.

    Recovers from a wedged run that left a session looking busy or made a new
    chat sit on 'waiting for another team run to finish' (the team run lock was
    held by a run that won't release it). Cancels every tracked run, clears the
    approval + steer gates, finishes every live-run buffer, and force-releases
    the team run-serialization lock. Idempotent and safe to hit any time."""
    from aiforge_core.runtime import (
        chat_approve, chat_cancel, chat_interject, chat_pipeline, chat_runs,
    )
    sessions = chat_cancel.cancel_all()
    for sid in sessions:
        chat_approve.cancel(sid)
        chat_approve.finish(sid)
        chat_interject.clear(sid)
        # NOTE: do NOT chat_cancel.finish(sid) here — that pops the cancel token
        # microseconds after cancel_all() set it, before the (slow, between-poll)
        # producer can observe it, so the run kept executing. Leave the token
        # SET; each run's own finally pops it once it has actually torn down.
    chat_runs.finish_all()
    lock_freed = chat_pipeline.force_release_run_lock()
    return {"killed": sessions, "count": len(sessions),
            "team_lock_released": lock_freed}


class _SteerBody(BaseModel):
    content: str = Field(..., description="mid-run guidance to fold in")


@app.post("/api/chat/sessions/{session_id}/steer")
def chat_session_steer(session_id: int, body: _SteerBody) -> dict:
    """Inject a steer message into the IN-FLIGHT run for this session WITHOUT
    stopping it (Gap A — mid-run steering). The message is queued and folded
    into the agent's working context at its next safe step, so the agent
    adjusts course mid-run. No-op (queued:false) for blank content.

    Drained by: simple/plan's ReAct loop, the parallel-team subtask loop
    (folds into SPEC.md), and the sequential team ADK driver's Doer/Refiner
    before_model callback (chat_steer_callback). Only best-of-N never
    drains, so steering there would queue a message no loop ever reads —
    detect that and report it unsupported rather than falsely claiming the
    steer was queued."""
    from aiforge_core.runtime import chat_interject
    # Atomic test-and-set: push() itself checks steerability under its lock, so
    # there's no window between the check and the enqueue for a run-end clear()
    # to slip a stale steer into the next turn (CC3).
    queued = chat_interject.push(session_id, body.content, require_steerable=True)
    if queued:
        return {"queued": True, "session_id": session_id}
    # Refused — distinguish blank content from a non-steerable (best-of-N) run.
    if not (body.content or "").strip():
        return {"queued": False, "session_id": session_id, "reason": "empty content"}
    return {"queued": False, "unsupported": True, "session_id": session_id,
            "reason": "steering not available for this run"}


class _ApproveBody(BaseModel):
    decision: str = Field(..., description="'approve' | 'reject'")
    id: int | None = Field(None, description="approval seq id echoed from the event")
    note: str | None = None


@app.post("/api/chat/sessions/{session_id}/approve")
def chat_session_approve(session_id: int, body: _ApproveBody) -> dict:
    """Resolve a pending approval gate (#1) — the chat run is blocked
    waiting for the user's Approve/Reject on a risky/ask-policy action."""
    from aiforge_core.runtime import chat_approve
    ok = chat_approve.resolve(session_id, body.decision, body.note or "", body.id)
    return {"resolved": ok, "decision": body.decision, "session_id": session_id}


# ── Rule / Memory / Feedback capture transparency ────────────────────────────

@app.get("/api/rules")
def list_captured_rules(repo: str | None = None,
                        session_id: int | None = None) -> dict:
    """Captured rules/memories/feedback for the transparency panel, grouped by
    scope. Optional ``repo`` / ``session_id`` filters."""
    from aiforge_core.runtime import rule_capture
    items = rule_capture.list_captured(repo=repo, session_id=session_id)
    by_scope: dict[str, list] = {}
    for it in items:
        by_scope.setdefault(it.get("scope") or "global", []).append(it)
    return {"items": items, "by_scope": by_scope}


class _RuleScopeBody(BaseModel):
    scope: str = Field(..., description="'global' | 'project' | 'session'")
    repo_root: str | None = Field(
        None, description="repo root so a →project rescope writes .aiforge/rules")


@app.put("/api/rules/{rule_id}/scope")
def rescope_captured_rule(rule_id: str, body: _RuleScopeBody) -> dict:
    """Re-file a captured item under a new scope (correcting a misclass). Any
    gate flag the rule enabled moves with it (and a deleted/undone one is
    revoked)."""
    from aiforge_core.runtime import rule_capture
    repo_root = body.repo_root or os.environ.get("AIFORGE_REPO_ROOT") or None
    return rule_capture.rescope(rule_id, body.scope, repo_root=repo_root)


@app.delete("/api/rules/{rule_id}")
def delete_captured_rule(rule_id: str) -> dict:
    """Undo a captured item — removes it from its store AND revokes any gate
    flag it enabled (so the approval gate is re-enabled)."""
    from aiforge_core.runtime import rule_capture
    return {"ok": rule_capture.undo(rule_id)}


# ── Explicit gate-disable flags (the EXPLICIT, scoped, revocable opt-in) ──────
#
# A gate is NEVER disabled by the classifier — only by an explicit user action
# through these endpoints. The capture path merely OFFERS the opt-in (gate_intent
# on the `captured` event); the UI pill calls POST here when the user clicks it.

@app.get("/api/rules/flags")
def list_gate_flags() -> dict:
    """Active gate-disable flags grouped by scope, for the Auto-approvals
    panel."""
    from aiforge_core.runtime import rule_capture
    return {"by_scope": rule_capture.list_flags()}


class _GateFlagBody(BaseModel):
    name: str = Field(..., description="'commit_auto_approve' | 'allow_delete'")
    scope: str = Field(..., description="'session' | 'project' (global needs confirm)")
    repo: str | None = None
    session_id: int | None = None
    rule_id: str | None = None
    allow_global: bool = False


@app.post("/api/rules/flags")
def set_gate_flag_ep(body: _GateFlagBody) -> dict:
    """EXPLICITLY enable a gate-disable flag for a scope (user-confirmed opt-in).
    Refuses global unless allow_global is set."""
    from aiforge_core.runtime import rule_capture
    return rule_capture.set_gate_flag(
        body.name, scope=body.scope, repo=body.repo,
        session_id=body.session_id, rule_id=body.rule_id,
        allow_global=body.allow_global)


@app.delete("/api/rules/flags/{name}")
def clear_gate_flag_ep(name: str, scope: str, repo: str | None = None,
                       session_id: int | None = None) -> dict:
    """Revoke a gate-disable flag for a scope (re-enables the gate)."""
    from aiforge_core.runtime import rule_capture
    ok = rule_capture.clear_gate_flag(name, scope=scope, repo=repo,
                                      session_id=session_id)
    return {"ok": ok}


class _CheckpointBody(BaseModel):
    label: str | None = Field(None, description="human label for the snapshot")


@app.get("/api/chat/sessions/{session_id}/checkpoints")
def chat_session_checkpoints(session_id: int) -> dict:
    """List workspace checkpoints (#3) for this session's working dir."""
    from aiforge_core.runtime import checkpoints, chat_store
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = session.get("cwd") or _default_cwd()
    return {"checkpoints": checkpoints.list_checkpoints(cwd)}


@app.post("/api/chat/sessions/{session_id}/checkpoints", status_code=201)
def chat_session_checkpoint_create(session_id: int, body: _CheckpointBody) -> dict:
    """Snapshot the session's working dir (#3) to a hidden git ref."""
    import datetime as _dt

    from aiforge_core.runtime import checkpoints, chat_store
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


@app.post("/api/chat/sessions/{session_id}/checkpoints/restore")
def chat_session_checkpoint_restore(session_id: int, body: _RestoreBody) -> dict:
    """Restore the session's working dir to a checkpoint (#3).

    Granularity: ``paths`` restores a subset; ``delete_orphans`` makes it a
    full-state restore (matching the snapshot exactly)."""
    from aiforge_core.runtime import checkpoints, chat_store
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = session.get("cwd") or _default_cwd()
    return checkpoints.restore(cwd, body.sha, paths=body.paths or None,
                               delete_orphans=bool(body.delete_orphans))


class _SessionTicketBody(BaseModel):
    content: str = Field(..., min_length=1)
    project: str | None = Field(None, description="target repo; defaults to session cwd name")


# ─────────────────────────── Integrations ───────────────────────────

class _ConfluenceCfg(BaseModel):
    base_url: str | None = None
    token: str | None = None       # write-only; omitted on read
    user: str | None = None
    insecure_tls: bool | None = None
    default_space: str | None = None   # auto-applied when a call omits `space`


@app.get("/api/integrations/confluence")
def integrations_confluence_get() -> dict:
    """Current Confluence settings (token masked). Reflects env override."""
    from aiforge_core.config import integrations
    stored = integrations.get("confluence")
    env_token = bool(os.environ.get("CONFLUENCE_TOKEN"))
    return {
        "base_url": os.environ.get("CONFLUENCE_BASE_URL") or stored.get("base_url", ""),
        "user": os.environ.get("CONFLUENCE_USER") or stored.get("user", ""),
        "insecure_tls": bool(stored.get("insecure_tls")),
        "has_token": env_token or bool(stored.get("token")),
        "default_space": os.environ.get("CONFLUENCE_DEFAULT_SPACE")
        or stored.get("default_space", ""),
        "env_managed": bool(os.environ.get("CONFLUENCE_BASE_URL") or env_token),
    }


@app.put("/api/integrations/confluence")
def integrations_confluence_set(body: _ConfluenceCfg) -> dict:
    """Persist Confluence settings. An empty/omitted token keeps the existing
    one (so re-saving the form doesn't wipe the secret)."""
    from aiforge_core.config import integrations
    patch: dict = {}
    if body.base_url is not None:
        patch["base_url"] = body.base_url.strip().rstrip("/")
    if body.user is not None:
        patch["user"] = body.user.strip()
    if body.insecure_tls is not None:
        patch["insecure_tls"] = bool(body.insecure_tls)
    if body.default_space is not None:
        patch["default_space"] = body.default_space.strip()
    if body.token:                       # only overwrite when a new token is given
        patch["token"] = body.token.strip()
    integrations.set_("confluence", patch)
    return integrations_confluence_get()


@app.post("/api/integrations/confluence/test")
def integrations_confluence_test() -> dict:
    """Live connectivity + auth check against the configured Confluence."""
    from aiforge_core.runtime.tools.confluence import confluence_test
    return confluence_test()


class _JiraCfg(BaseModel):
    base_url: str | None = None
    token: str | None = None       # write-only; omitted on read
    user: str | None = None
    insecure_tls: bool | None = None
    default_project: str | None = None  # auto-applied when a call omits `project`


@app.get("/api/integrations/jira")
def integrations_jira_get() -> dict:
    """Current Jira settings (token masked). Reflects env override."""
    from aiforge_core.config import integrations
    stored = integrations.get("jira")
    env_token = bool(os.environ.get("JIRA_TOKEN"))
    return {
        "base_url": os.environ.get("JIRA_BASE_URL") or stored.get("base_url", ""),
        "user": os.environ.get("JIRA_USER") or stored.get("user", ""),
        "insecure_tls": bool(stored.get("insecure_tls")),
        "has_token": env_token or bool(stored.get("token")),
        "default_project": os.environ.get("JIRA_DEFAULT_PROJECT")
        or stored.get("default_project", ""),
        "env_managed": bool(os.environ.get("JIRA_BASE_URL") or env_token),
    }


@app.put("/api/integrations/jira")
def integrations_jira_set(body: _JiraCfg) -> dict:
    """Persist Jira settings. An empty/omitted token keeps the existing one
    (so re-saving the form doesn't wipe the secret)."""
    from aiforge_core.config import integrations
    patch: dict = {}
    if body.base_url is not None:
        patch["base_url"] = body.base_url.strip().rstrip("/")
    if body.user is not None:
        patch["user"] = body.user.strip()
    if body.insecure_tls is not None:
        patch["insecure_tls"] = bool(body.insecure_tls)
    if body.default_project is not None:
        patch["default_project"] = body.default_project.strip()
    if body.token:                       # only overwrite when a new token is given
        patch["token"] = body.token.strip()
    integrations.set_("jira", patch)
    return integrations_jira_get()


@app.post("/api/integrations/jira/test")
def integrations_jira_test() -> dict:
    """Live connectivity + auth check against the configured Jira."""
    from aiforge_core.runtime.tools.jira import jira_test
    return jira_test()


class _GitlabCfg(BaseModel):
    base_url: str | None = None
    token: str | None = None       # write-only; omitted on read
    project: str | None = None     # default project (id or "group/proj")
    oauth: bool | None = None      # token sent as Bearer instead of PRIVATE-TOKEN
    insecure_tls: bool | None = None


@app.get("/api/integrations/gitlab")
def integrations_gitlab_get() -> dict:
    """Current GitLab settings (token masked). Reflects env override."""
    from aiforge_core.config import integrations
    stored = integrations.get("gitlab")
    env_token = bool(os.environ.get("GITLAB_TOKEN"))
    return {
        "base_url": os.environ.get("GITLAB_BASE_URL") or stored.get("base_url", ""),
        "project": os.environ.get("GITLAB_PROJECT") or stored.get("project", ""),
        "oauth": bool(stored.get("oauth")),
        "insecure_tls": bool(stored.get("insecure_tls")),
        "has_token": env_token or bool(stored.get("token")),
        "env_managed": bool(os.environ.get("GITLAB_BASE_URL") or env_token),
    }


@app.put("/api/integrations/gitlab")
def integrations_gitlab_set(body: _GitlabCfg) -> dict:
    """Persist GitLab settings. An empty/omitted token keeps the existing one
    (so re-saving the form doesn't wipe the secret)."""
    from aiforge_core.config import integrations
    patch: dict = {}
    if body.base_url is not None:
        patch["base_url"] = body.base_url.strip().rstrip("/")
    if body.project is not None:
        patch["project"] = body.project.strip()
    if body.oauth is not None:
        patch["oauth"] = bool(body.oauth)
    if body.insecure_tls is not None:
        patch["insecure_tls"] = bool(body.insecure_tls)
    if body.token:                       # only overwrite when a new token is given
        patch["token"] = body.token.strip()
    integrations.set_("gitlab", patch)
    return integrations_gitlab_get()


@app.post("/api/integrations/gitlab/test")
def integrations_gitlab_test() -> dict:
    """Live connectivity + auth check against the configured GitLab."""
    from aiforge_core.runtime.tools.gitlab import gitlab_test
    return gitlab_test()


class _EmailCfg(BaseModel):
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None   # write-only; omitted on read
    smtp_from: str | None = None
    smtp_starttls: bool | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    imap_user: str | None = None
    imap_password: str | None = None   # write-only; omitted on read
    imap_ssl: bool | None = None


@app.get("/api/integrations/email")
def integrations_email_get() -> dict:
    """Current Email (SMTP/IMAP) settings (passwords masked). Reflects env
    override — an env-set host/password wins over the stored value."""
    from aiforge_core.config import integrations
    stored = integrations.get("email")
    env_smtp_pw = bool(os.environ.get("AIFORGE_SMTP_PASSWORD"))
    env_imap_pw = bool(os.environ.get("AIFORGE_IMAP_PASSWORD"))
    env_managed = bool(
        os.environ.get("AIFORGE_SMTP_HOST") or os.environ.get("AIFORGE_IMAP_HOST")
        or env_smtp_pw or env_imap_pw)
    return {
        "smtp_host": os.environ.get("AIFORGE_SMTP_HOST") or stored.get("smtp_host", ""),
        "smtp_port": int(os.environ.get("AIFORGE_SMTP_PORT") or stored.get("smtp_port") or 587),
        "smtp_user": os.environ.get("AIFORGE_SMTP_USER") or stored.get("smtp_user", ""),
        "smtp_from": os.environ.get("AIFORGE_SMTP_FROM") or stored.get("smtp_from", ""),
        "smtp_starttls": _env_truthy("AIFORGE_SMTP_STARTTLS")
                         if os.environ.get("AIFORGE_SMTP_STARTTLS") else bool(stored.get("smtp_starttls", True)),
        "imap_host": os.environ.get("AIFORGE_IMAP_HOST") or stored.get("imap_host", ""),
        "imap_port": int(os.environ.get("AIFORGE_IMAP_PORT") or stored.get("imap_port") or 993),
        "imap_user": os.environ.get("AIFORGE_IMAP_USER") or stored.get("imap_user", ""),
        "imap_ssl": _env_truthy("AIFORGE_IMAP_SSL")
                    if os.environ.get("AIFORGE_IMAP_SSL") else bool(stored.get("imap_ssl", True)),
        "has_smtp_password": env_smtp_pw or bool(stored.get("smtp_password")),
        "has_imap_password": env_imap_pw or bool(stored.get("imap_password")),
        "env_managed": env_managed,
    }


@app.put("/api/integrations/email")
def integrations_email_set(body: _EmailCfg) -> dict:
    """Persist Email (SMTP/IMAP) settings. An empty/omitted password keeps the
    existing one (so re-saving the form doesn't wipe the secret)."""
    from aiforge_core.config import integrations
    patch: dict = {}
    for f in ("smtp_host", "smtp_user", "smtp_from", "imap_host", "imap_user"):
        v = getattr(body, f)
        if v is not None:
            patch[f] = v.strip()
    for f in ("smtp_port", "imap_port"):
        v = getattr(body, f)
        if v is not None:
            patch[f] = int(v)
    for f in ("smtp_starttls", "imap_ssl"):
        v = getattr(body, f)
        if v is not None:
            patch[f] = bool(v)
    if body.smtp_password:               # only overwrite when a new secret is given
        patch["smtp_password"] = body.smtp_password
    if body.imap_password:
        patch["imap_password"] = body.imap_password
    integrations.set_("email", patch)
    return integrations_email_get()


@app.post("/api/integrations/email/test")
def integrations_email_test() -> dict:
    """Live connectivity + auth check against the configured SMTP/IMAP."""
    from aiforge_core.runtime.tools.email_tool import email_test
    return email_test()


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
                       "aiforge-graph-mcp"),
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
    except TimeoutError:
        try:
            proc.kill()
            await proc.wait()   # reap — don't leak a zombie
        except Exception: pass
        raise HTTPException(504, "MCP server timed out")
    except FileNotFoundError:
        # No MCP server binary installed (operator reset 2026-06-26). Fail
        # soft so the UI shows a clean empty state instead of a 500.
        return {"ok": False, "error": "MCP not configured",
                "detail": f"binary not found: {cmd[0]}"}

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
    """Per-step perf snapshot, backed by the ndjson perf recorder.

    Samples are appended by ``aiforge_core.runtime.perf_recorder`` at the LLM
    call boundary and at each chat/doer tool dispatch. ``reset`` truncates the
    recorder's ndjson and returns an empty snapshot."""
    from aiforge_core.runtime import perf_recorder
    if reset:
        perf_recorder.reset()
        return {"rows": [], "reset": True}
    return {"rows": perf_recorder.aggregate(), "reset": False}


def _static_topology() -> dict:
    """Static v6 pipeline DAG — fallback when no live topology module is
    present, so the Workflow view renders instead of erroring."""
    # Mirror the live v6 pipeline order (runtime.workflow_topology). Linear
    # projection of the real DAG: triage → enhancer → context/research →
    # planner → verifier → doer loop → validator → learner.
    stages = ["triage", "enhancer", "researcher", "planner", "verifier",
              "doer", "refiner", "feedback", "validator", "learner"]
    nodes = [{"id": s, "label": s, "type": "agent", "tools": [],
              "status": "idle", "last_event_at": None,
              "skills": [], "rules": [], "workflows": []} for s in stages]
    edges = [{"from": stages[i], "to": stages[i + 1], "label": ""}
             for i in range(len(stages) - 1)]
    return {"nodes": nodes, "edges": edges, "ticket": None, "static": True,
            "context": {"skills": [], "rules": [], "workflows": []}}


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
    _rs.upsert(name, **{k: v for k, v in body.model_dump().items()
                        if v is not None})
    return repo_standards_get(name=name)


# ─────────────────────── Ticket file attachments ────────────────────────
# Operator-uploaded files persisted by ``_persist_ticket_attachments``
# under ``{AIFORGE_REPO_ROOT}/.aiforge/ticket-files/{identifier}/``.
# Mount as a static route so the UI can render image thumbnails inline
# and offer download links for non-image files. Names were sanitized at
# upload (``_Path(f.name).name``) so path-traversal is contained to the
# per-ticket subdir.
# Serve from the SAME persistent base uploads are written to
# (``_ticket_files_base``) — previously this used AIFORGE_REPO_ROOT, which in
# Docker pointed at an ephemeral HOME dir, so attachments 404'd after any
# container recreate.
_TICKET_FILES_ROOT = str(_ticket_files_base())
try:
    os.makedirs(_TICKET_FILES_ROOT, exist_ok=True)
except OSError:
    # Never let an unwritable attachments dir crash API boot; the mount uses
    # check_dir=False and uploads makedirs(parents=True) on demand.
    pass
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
