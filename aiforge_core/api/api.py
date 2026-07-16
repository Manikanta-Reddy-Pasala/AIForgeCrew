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
from aiforge_core.runtime.background import spawn as _spawn
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

# Domain route modules split out of this file (see aiforge_core/api/routes/).
# They only import shared helpers + runtime modules (never api.py), so including
# them here is import-safe.
from aiforge_core.api.routes import jobs as _r_jobs  # noqa: E402
from aiforge_core.api.routes import repos as _r_repos  # noqa: E402
from aiforge_core.api.routes import library as _r_library  # noqa: E402
from aiforge_core.api.routes import rules as _r_rules  # noqa: E402
from aiforge_core.api.routes import mcp as _r_mcp  # noqa: E402
from aiforge_core.api.routes import integrations as _r_integrations  # noqa: E402
from aiforge_core.api.routes import memory as _r_memory  # noqa: E402
from aiforge_core.api.routes import agents as _r_agents  # noqa: E402
from aiforge_core.api.routes import chat as _r_chat  # noqa: E402
app.include_router(_r_jobs.router)
app.include_router(_r_repos.router)
app.include_router(_r_library.router)
app.include_router(_r_rules.router)
app.include_router(_r_mcp.router)
app.include_router(_r_integrations.router)
app.include_router(_r_memory.router)
app.include_router(_r_agents.router)
app.include_router(_r_chat.router)

# Backwards-compat re-exports: private chat helpers relocated into
# aiforge_core.api.routes.chat but still imported by name from
# aiforge_core.api.api (tests). Keep them reachable at the old path.
from aiforge_core.api.routes.chat import (  # noqa: E402,F401
    _chat_history_for_agent,
    _delete_chat_workspace,
    _step_digest,
)


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


# Postgres/Neo4j pointers from a prior HYBRID setup that may still linger in
# runtime.env — this build is SQLite-only, so restoring them would make tickets/
# chat/memory try a Postgres/Neo4j that no longer exists ("Postgres unreachable"
# spam). Never restore them (unless AIFORGE_KEEP_PG=1 for a real external PG).
_RUNTIME_ENV_DB_KEYS = frozenset({
    "AIFORGE_PG_URL", "AIFORGE_DSN", "AIFORGE_FORCE_PG", "AIFORGE_PGMEM_DSN",
    "AIFORGE_NEO4J_URI", "NEO4J_URI", "AIFORGE_NEO4J_USER",
    "AIFORGE_NEO4J_PASSWORD", "AIFORGE_NEO4J_PASS",
    "AIFORGE_REQUIRE_DATA_BACKEND", "AIFORGE_MEMORY_BACKEND",
})


@app.on_event("startup")
def _load_runtime_env() -> None:
    """Restore UI-persisted toggles (runtime.env) into the process env on boot
    using a plain KEY=VALUE parser — NOT a shell source — so a value can never
    be executed. A real env var / project .env already in the environment WINS
    (setdefault), keeping them the operator's explicit escape hatch. Stale
    Postgres/Neo4j backend keys are SKIPPED (single mode is SQLite)."""
    try:
        path = _RUNTIME_ENV_PATH
        if not os.path.isfile(path):
            return
        _keep_pg = os.environ.get("AIFORGE_KEEP_PG") == "1"
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                if k in _RUNTIME_ENV_DB_KEYS and not _keep_pg:
                    continue                          # SQLite-only; ignore stale DB pointers
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

    _spawn(_work, name="ctx-reload")


@app.on_event("startup")
@app.on_event("startup")
def _check_tool_parity() -> None:
    """Warn (loudly, on the box) if a cross-surface tool drifted between the
    chat + Doer registries — the recurring 'works in chat, not in pipeline' bug.
    Startup check, not just CI. Never blocks startup."""
    try:
        from aiforge_core.runtime import tool_manifest
        tool_manifest.validate_or_warn()
    except Exception:  # noqa: BLE001
        pass


@app.on_event("startup")
def _reassign_agents_on_boot() -> None:
    """Re-apply capability-based agent→model assignment on every boot (when
    auto-assign is on, the default) so EXISTING configs pick up mapping fixes —
    e.g. quick roles (enhancer/learner) moving OFF a reasoning model that returns
    empty, ONTO the fast model. Manual mode (AIFORGE_AUTO_ASSIGN_AGENTS=0) is
    left untouched. Best-effort; never blocks startup."""
    try:
        _r_agents._reassign_by_capability()
    except Exception:  # noqa: BLE001
        pass


@app.on_event("startup")
def _run_memory_migrations() -> None:
    """Auto-upgrade EVERY deployment's memory into the current scoped-OKR shape:
    legacy brief format → OKR envelope, compacted briefs → OKR learnings, old
    Neo4j Observation/Decision nodes → md captures, then flat okr/ → global/ +
    projects/<repo>/. Idempotent (one-shot steps are marker-guarded); never
    blocks startup. This is the migration path new/upgrading users get for free
    on ``run.sh`` (which boots this API)."""
    def _run():
        try:
            from aiforge_core.memory import migrations
            r = migrations.run_startup_migrations()
            _af_log.info("memory migrations: %s",
                         {k: (v.get("moved") or v.get("migrated")
                              or v.get("skipped") or v.get("ok"))
                          for k, v in r.items()})
        except Exception:  # noqa: BLE001 — migration is best-effort
            pass
    # background thread: the classify step calls the LLM, which must not delay
    # the API coming up. Migrations are idempotent + marker-guarded.
    try:
        _spawn(_run, name="memory-migrations")
    except Exception:  # noqa: BLE001
        _run()


def _start_jobs_scheduler() -> None:
    """Scheduled-jobs tick loop — daemon thread, same pattern as the
    other background workers. AIFORGE_JOBS_DISABLE=1 skips it."""
    try:
        import threading

        from aiforge_core.jobs import scheduler as jobs_scheduler
        if jobs_scheduler._disabled():
            return
        _spawn(jobs_scheduler.run_loop, name="jobs-scheduler")
    except Exception:  # noqa: BLE001 — startup must never crash the API
        pass


@app.on_event("startup")
def _start_daily_reindex() -> None:
    """Once a day, re-index EVERY registered repo/docs source so semantic
    recall + the graphify graph stay current with the code (the RepoMap is
    already on-the-fly fresh; this refreshes the chunk/graph layers). Runs at
    AIFORGE_REINDEX_HOUR (local, default 03:00). Off with
    AIFORGE_REINDEX_DAILY=0 or AIFORGE_JOBS_DISABLE=1."""
    if os.environ.get("AIFORGE_REINDEX_DAILY", "1") in ("0", "false", "no"):
        return
    if os.environ.get("AIFORGE_JOBS_DISABLE", "") in ("1", "true", "yes"):
        return
    try:
        hour = max(0, min(23, int(os.environ.get("AIFORGE_REINDEX_HOUR", "3"))))
    except ValueError:
        hour = 3
    from aiforge_core.runtime import periodic as _pd
    # Run the INCREMENTAL reindex frequently (default every 3h), not once a day,
    # so all indexed layers (chunks + tree-sitter symbols + graphify) refresh
    # within hours of a commit. Cheap: reindex_all merkle-skips unchanged repos,
    # so an idle tick is a near-instant no-op; only a CHANGED repo pays.
    try:
        every_h = max(1, int(os.environ.get("AIFORGE_REINDEX_EVERY_H", "3")))
    except ValueError:
        every_h = 3
    _pd.register("reindex", _r_memory._spawn_reindex_all, every_s=every_h * 3600)

    # HOURLY CHAT-MD COMPACTION — per-turn writes append forever to
    # ~/.aiforge/memory/*.md; md_store.compact() consolidates them (map-reduce
    # summary, archives originals) so the memory folder stays bounded + legible.
    # Was manual-only (POST /api/memory/files/compact); now scheduled hourly
    # (AIFORGE_COMPACT_EVERY_H) so memory stays organized-by-topic within the hour.
    def _compact_chat_md() -> None:
        # Two axes, both kept (overlap intended): per-REPO → the project brief
        # you load when opening a repo; per-TOPIC → cross-repo theme notes.
        try:
            from aiforge_core.memory import md_store
            # Order matters: REPO first as a non-destructive projection
            # (archive_sources=False) so every unit is folded into its project
            # brief while the raw file still exists. TOPIC runs second and
            # ARCHIVES the folded raw units (archive_sources=True) — so memory is
            # organized BY TOPIC and the per-session raw notes stop piling up in
            # the live folder (moved to archive/<ts>/, reversible). Both briefs
            # re-feed their own consolidated OKR sections on the next run, so a
            # unit's knowledge survives in both briefs after its raw file clears.
            # min_group=1: fold even a LONE note into its brief — a single
            # session is often its own topic, so min_group=2 would leave it
            # sitting raw forever ("nothing to compact"). Singletons still get
            # organized by topic + archived.
            r_repo = md_store.compact(group_by="repo", min_group=1, summarize=True,
                                      model_role="learner", archive_sources=False)
            r_topic = md_store.compact(group_by="topic", min_group=1, summarize=True,
                                       model_role="learner", archive_sources=True)
            # Retire per-run captures that masquerade as canonical briefs
            # (compacted-<desc>-YYYYMMDD-hex.md) — compact() can never see them,
            # so they'd pile up forever; their facts already live in the real
            # compacted-<topic>.md brief. Archive them out (reversible).
            r_sweep = md_store.sweep_stale_captures(archive=True)
            # Retire DEAD briefs — a compacted-<key>.md left with only the
            # boilerplate Objective (facts migrated elsewhere / emptied /
            # compacted-compacted-* artifact). They read as "empty" memories.
            r_empty = md_store.sweep_empty_briefs(archive=True)
            # Apply the CROSS-BRIEF rules on every compaction (not just Compact
            # all): merge topics, drop global-dup facts, resolve contradictions
            # (latest wins), sweep emptied stubs, lint + (re)link briefs. Without
            # this the hourly/Compact path never linked or deduped across briefs.
            r_rules = md_store.finalize_briefs(role="learner", recent_only=True)
            _af_log.info("md brief: repo=%s topic=%s sweep=%s empty=%s rules=%s",
                         r_repo, r_topic, r_sweep, r_empty, r_rules)
        except Exception as exc:  # noqa: BLE001
            _af_log.warning("md compaction failed: %s", exc)

    try:
        _compact_every_h = max(1, int(os.environ.get("AIFORGE_COMPACT_EVERY_H", "1")))
    except (TypeError, ValueError):
        _compact_every_h = 1
    _pd.register("chat-compact", _compact_chat_md,
                 every_s=_compact_every_h * 3600)   # hourly by default

    # Daily GRAPH MAINTENANCE (Neo4j only) — AFM decay + per-repo digest/dedupe;
    # no-op on the embedded backend. Best-effort.
    def _graph_maintain() -> None:
        try:
            from aiforge_core.memory import backend_select
            if backend_select.memory_backend() != "neo4j":
                return
            import argparse

            from aiforge_memory.api.commands import maintain as _mt
            _mt.run(argparse.Namespace())
            _af_log.info("graph maintenance ran")
        except Exception as exc:  # noqa: BLE001
            _af_log.debug("graph maintenance skipped/failed: %s", exc)

    _pd.register("graph-maintain", _graph_maintain,
                 at_hour=max(0, min(23, hour + 2)))

    # Daily SEMANTIC DEDUP of the embedded memory store — write_unit only dedups
    # exact (repo,text); paraphrases pile up. Collapses near-duplicates on the
    # stored vectors (no sidecar). Neo4j has its own write-time semantic dedupe.
    def _dedupe_memory() -> None:
        try:
            from aiforge_core.memory import backend_select
            if backend_select.memory_backend() != "sqlite":
                return
            from aiforge_core.memory import sqlite_memory
            r = sqlite_memory.dedupe()
            _af_log.info("memory dedup: %s", r)
        except Exception as exc:  # noqa: BLE001
            _af_log.warning("memory dedup failed: %s", exc)

    _pd.register("memory-dedup", _dedupe_memory,
                 at_hour=max(0, min(23, hour + 3)))

    # Daily FULL RECOMPACT — the hourly chat-compact only folds briefs with NEW
    # live captures; a fact-only brief whose topic saw no new note keeps raw
    # Facts in its inbox, never LLM-consolidated into prose. Once a day, force a
    # full recompact so EVERY brief is re-folded through the model (dedupe /
    # supersede / re-map its accumulated facts), then dedupe + repo-profiles +
    # reingest. Heavy (LLM per brief) → daily, off-peak, opt-out via
    # AIFORGE_RECOMPACT_DAILY=0. Serializes against manual compact-all on
    # _COMPACT_LOCK, so overlap is safe.
    def _recompact_all() -> None:
        try:
            from aiforge_core.memory import migrations
            r = migrations.force_recompact_all()
            _af_log.info("daily recompact-all: %s", r)
        except Exception as exc:  # noqa: BLE001
            _af_log.warning("daily recompact-all failed: %s", exc)

    # Fires at a NIGHT local hour (AIFORGE_RECOMPACT_HOUR, default 02:00 local) —
    # a dedicated knob, NOT tied to the reindex hour, so the heavy nightly
    # compact-all lands off-peak regardless of when reindex runs.
    try:
        _recompact_hour = max(0, min(23, int(
            os.environ.get("AIFORGE_RECOMPACT_HOUR", "2"))))
    except (TypeError, ValueError):
        _recompact_hour = 2
    if os.environ.get("AIFORGE_RECOMPACT_DAILY", "1") != "0":
        _pd.register("recompact-all", _recompact_all,
                     at_hour=_recompact_hour)

    # Session-end OKR compaction — IDLE trigger. AIFORGE_SESSION_COMPACT selects
    # the trigger (idle | turns | explicit | off); the daemon only runs the idle
    # scan. Idle is detected without parsing message timestamps: a session whose
    # message count is UNCHANGED across two consecutive scans (spaced
    # AIFORGE_SESSION_IDLE_MIN apart) has gone quiet → compact it once. State is
    # in-process (resets on restart, which is fine — an active session just waits
    # one more idle window).
    _session_scan_state: dict = {}

    def _compact_idle_sessions() -> None:
        if os.environ.get("AIFORGE_SESSION_COMPACT", "idle") != "idle":
            return
        try:
            from aiforge_core.runtime import chat_okr, chat_store
            from aiforge_core.runtime.chat_agent import _chat_repo_key
            sessions = chat_store.list_sessions() or []
        except Exception as exc:  # noqa: BLE001
            _af_log.debug("session-okr scan setup failed: %s", exc)
            return
        for s in sessions:
            sid = (s or {}).get("id")
            if sid is None:
                continue
            try:
                count = len(chat_store.get_messages(sid) or [])
            except Exception:  # noqa: BLE001
                continue
            prev = _session_scan_state.get(sid)
            if count > 0 and prev is not None \
                    and prev.get("count") == count and not prev.get("done"):
                cwd = (s or {}).get("cwd")
                repo = _chat_repo_key(cwd) if cwd else None
                try:
                    r = chat_okr.compact_session(sid, repo=repo)
                    _af_log.info("idle session compact sid=%s: %s", sid, r)
                except Exception as exc:  # noqa: BLE001
                    _af_log.warning("idle session compact sid=%s failed: %s", sid, exc)
                _session_scan_state[sid] = {"count": count, "done": True}
            elif prev is None or prev.get("count") != count:
                _session_scan_state[sid] = {"count": count, "done": False}
        live = {(s or {}).get("id") for s in sessions}
        for sid in list(_session_scan_state):
            if sid not in live:
                _session_scan_state.pop(sid, None)

    try:
        _idle_min = max(1, int(os.environ.get("AIFORGE_SESSION_IDLE_MIN", "30")))
    except (TypeError, ValueError):
        _idle_min = 30
    _pd.register("session-okr-compact", _compact_idle_sessions,
                 every_s=max(300, _idle_min * 60))
    _pd.start()


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
# _db() lives in aiforge_core.api._shared (single source shared with route
# modules, which cannot import api.py); re-exported here as the module-local
# name every handler already uses.
from aiforge_core.api._shared import _db  # noqa: E402


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
    # Langfuse trace mirror (optional): expose ONLY the host so the UI can
    # render a "Traces ↗" link — never the keys.
    try:
        from aiforge_core.integrations import langfuse_adapter as _lfa
        if _lfa.enabled():
            status["traces_url"] = os.environ.get("LANGFUSE_HOST", "")
    except Exception:  # noqa: BLE001
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


# (scheduled-jobs routes moved to aiforge_core.api.routes.jobs — included below)


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


# ─── Playbook Library (skills/workflows/rules) + /api/workflows list →
#     moved to aiforge_core.api.routes.library (APIRouter).


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

    _spawn(_bg, name=f"parallel-{identifier}")
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


from aiforge_core.api._shared import env_truthy as _env_truthy  # noqa: E402


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


# ───────────────────── Memory sources (ingestion) ──────────────────────
# Register code repos / docs folders / URLs / files and index them into
# the active memory backend. See aiforge_core.runtime.memory_sources +
# memory_ingest.


# ── Markdown-file memory (human-readable notes on disk + searchable) ──


# ─────────────────── Memory admin (overview + clear) ───────────────────────
# High-level visibility into every memory datasource + a DESTRUCTIVE "empty
# this store" per datasource. These DELETE indexed DATA (graph nodes / SQLite
# units / on-disk notes / chat history) but NEVER the registered sources or
# config. They inherit the API-token auth (middleware); each also requires an
# explicit ``{"confirm": true}`` body so an accidental click can't wipe data.


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


# ─────────────────────── Agent model config v2 ─────────────────────────
# v2 surface for the new Settings UI. Adds per-role base_url, returns the
# full model catalog inline with each provider, and exposes only the 6
# v5 archetype roles. v1 (above) kept untouched so the current UI build
# keeps working until it migrates.


# ── Model registry (simplified Settings: add models once, agents pick one) ────


# ─── MCP marketplace/installer + servers routes → aiforge_core.api.routes.mcp


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


# ───────────────────── Persistent chat sessions ────────────────────────
# Claude-style multi-conversation chat: server-stored sessions the user
# can resume/rename/delete. Each session threads full history into the
# agent so a topic continues across turns. Storage: aiforge_core.runtime.
# chat_store (SQLite, survives reloads/redeploys).


# Orchestrator = the 2 layer-1 agents (enhancer + planner) that analyze/enhance
# the request and split it into subtasks. Lets you run the splitter on a
# different (e.g. stronger reasoning) model than the workers.


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


# ── Rule / Memory / Feedback capture transparency ────────────────────────────

# ─── Captured-rules + gate-flag routes → aiforge_core.api.routes.rules


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


# ─── /api/mcp/tool → aiforge_core.api.routes.mcp


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
@app.get("/files/{identifier}/{name}")
def serve_ticket_file(identifier: str, name: str):
    """Serve a ticket attachment by (ticket, filename).

    A dynamic route rather than a ``StaticFiles`` mount: the mount binds ONE
    directory at import time, but the runner rebinds ``AIFORGE_REPO_ROOT`` per
    ticket, so uploads land in a per-ticket worktree
    (``/home/ai/codeRepo/<repo>/.aiforge/ticket-files/...``) that the boot-time
    mount root does not point at → every such attachment 404'd. The ticket's
    ``metadata.attached_files[].abs_path`` records the real write location, so
    resolve from there first, then fall back to the persistent base dir.
    """
    from pathlib import Path as _Path
    safe_name = _Path(name).name  # contain path traversal to the ticket dir
    candidates: list[_Path] = []
    try:
        t = tickets_mod.get_enriched(identifier)
    except Exception:  # noqa: BLE001 — a store hiccup must not 500 the asset
        t = None
    if t:
        for f in ((t.get("metadata") or {}).get("attached_files") or []):
            if not isinstance(f, dict) or (f.get("name") or "") != safe_name:
                continue
            ap = f.get("abs_path")
            if ap:
                candidates.append(_Path(ap))
    # Fallbacks: the persistent base (current env) + the boot-time mount root.
    candidates.append(_ticket_files_base() / identifier / safe_name)
    candidates.append(_Path(_TICKET_FILES_ROOT) / identifier / safe_name)
    for p in candidates:
        try:
            if p.is_file():
                return FileResponse(str(p))
        except OSError:
            continue
    raise HTTPException(404, f"attachment {identifier}/{safe_name} not found")

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
