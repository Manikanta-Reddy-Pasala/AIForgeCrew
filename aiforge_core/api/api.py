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
from datetime import UTC, date as _date
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
from aiforge_core.api.routes.mcp import _MCP_ALLOWED_TOOLS  # noqa: E402 — used by _call_mcp_sync (moved with the mcp routes)
from aiforge_core.api.routes import integrations as _r_integrations  # noqa: E402
from aiforge_core.api.routes import memory as _r_memory  # noqa: E402
from aiforge_core.api.routes import agents as _r_agents  # noqa: E402
from aiforge_core.api.routes import chat as _r_chat  # noqa: E402
from aiforge_core.api.routes import tickets as _r_tickets  # noqa: E402
from aiforge_core.api.routes import runtime as _r_runtime  # noqa: E402
from aiforge_core.api.routes import observability as _r_observability  # noqa: E402
from aiforge_core.api.routes import files as _r_files  # noqa: E402
from aiforge_core.api.routes import sync as _r_sync  # noqa: E402
from aiforge_core.api.routes import admin as _r_admin  # noqa: E402
app.include_router(_r_jobs.router)
app.include_router(_r_repos.router)
app.include_router(_r_library.router)
app.include_router(_r_rules.router)
app.include_router(_r_mcp.router)
app.include_router(_r_integrations.router)
app.include_router(_r_memory.router)
app.include_router(_r_agents.router)
app.include_router(_r_chat.router)
app.include_router(_r_tickets.router)
app.include_router(_r_runtime.router)
app.include_router(_r_observability.router)
app.include_router(_r_files.router)
app.include_router(_r_sync.router)
app.include_router(_r_admin.router)

# Backwards-compat re-exports: private chat helpers relocated into
# aiforge_core.api.routes.chat but still imported by name from
# aiforge_core.api.api (tests). Keep them reachable at the old path.
from aiforge_core.api.routes.chat import (  # noqa: E402,F401
    _chat_history_for_agent,
    _delete_chat_workspace,
    _step_digest,
)
# Ticket + file helpers/models relocated into their route modules but still
# referenced by name from aiforge_core.api.api (tests). Keep them reachable.
from aiforge_core.api.routes.tickets import (  # noqa: E402,F401
    AttachedFile,
    _persist_ticket_attachments,
    _remove_ticket_attachments,
)
from aiforge_core.api.routes.files import serve_ticket_file  # noqa: E402,F401
# agent_config re-export — the agents/chat config surface moved to route
# modules, but tests still reach it as aiforge_core.api.api._acfg.
from aiforge_core.config import agent_config as _acfg  # noqa: E402,F401


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
        from aiforge_core.api._shared import _RUNTIME_ENV_PATH
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


def _compact_at_hour() -> "int | None":
    """Local hour for the single daily memory-compaction pass, or None to keep
    the old hourly/idle/nightly schedule.

    Default 18 (evening): every fold costs learner-LLM calls, so re-folding the
    same briefs all day buys little over one pass once the day's work is in.
    ``AIFORGE_COMPACT_AT_HOUR=off`` (or an explicit ``AIFORGE_COMPACT_EVERY_H``,
    which only means anything on the hourly schedule) restores the old cadence.

    Parsing lives in ``runtime.compact_window`` so the opportunistic chat folds
    read the SAME window as this scheduled pass.
    """
    from aiforge_core.runtime import compact_window
    return compact_window.at_hour()


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
    def _compact_chat_md() -> bool:
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
            return True
        except Exception as exc:  # noqa: BLE001
            _af_log.warning("md compaction failed: %s", exc)
            return False

    # ONE COMPACTION A DAY, IN THE EVENING (default). Every local fold is
    # LLM-heavy, and running them hourly / per idle session spends tokens all
    # day re-folding briefs that barely moved. AIFORGE_COMPACT_AT_HOUR (local,
    # default 18) collapses chat-compact + the session OKR fold + the full
    # recompact into ONE evening pass; set it to "off" to go back to the old
    # hourly/idle/nightly schedule.
    _daily_hour = _compact_at_hour()
    if _daily_hour is None:
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
    def _recompact_all() -> bool:
        try:
            from aiforge_core.memory import migrations
            r = migrations.force_recompact_all()
            _af_log.info("daily recompact-all: %s", r)
            return True
        except Exception as exc:  # noqa: BLE001
            _af_log.warning("daily recompact-all failed: %s", exc)
            return False

    # Fires at a NIGHT local hour (AIFORGE_RECOMPACT_HOUR, default 02:00 local) —
    # a dedicated knob, NOT tied to the reindex hour, so the heavy nightly
    # compact-all lands off-peak regardless of when reindex runs.
    try:
        _recompact_hour = max(0, min(23, int(
            os.environ.get("AIFORGE_RECOMPACT_HOUR", "2"))))
    except (TypeError, ValueError):
        _recompact_hour = 2
    _recompact_on = os.environ.get("AIFORGE_RECOMPACT_DAILY", "1") != "0"
    if _recompact_on and _daily_hour is None:
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
    try:
        _max_windows = max(1, int(os.environ.get(
            "AIFORGE_SESSION_COMPACT_MAX_WINDOWS", "20")))
    except (TypeError, ValueError):
        _max_windows = 20

    def _compact_idle_sessions(idle_only: bool = True) -> bool:
        # idle_only=False (the daily pass): fold EVERY session that has new
        # turns. compact_session is offset-based, so a session still in flight
        # loses nothing — tomorrow's pass picks up the turns added after this
        # one. The two-scan idle handshake can't work on a once-a-day cadence
        # (it would defer every session by a full day).
        mode = os.environ.get("AIFORGE_SESSION_COMPACT", "idle")
        if mode in ("off", "0", "false", "no"):
            return True                      # off by config, not a failure
        if idle_only and mode != "idle":
            return True                      # the idle daemon only runs for 'idle'
        # The DAILY pass folds for every other mode too: it IS the trigger, and
        # 'turns'/'explicit' with no idle daemon left would mean nothing folds.
        try:
            from aiforge_core.runtime import chat_okr, chat_store
            from aiforge_core.runtime.chat_agent import _chat_repo_key
            sessions = chat_store.list_sessions() or []
        except Exception as exc:  # noqa: BLE001
            _af_log.warning("session-okr scan setup failed: %s", exc)
            return False
        failed = False
        for s in sessions:
            sid = (s or {}).get("id")
            if sid is None:
                continue
            try:
                count = len(chat_store.get_messages(sid) or [])
            except Exception:  # noqa: BLE001
                continue
            stamp = str((s or {}).get("created_at") or (s or {}).get("started_at")
                        or "")
            prev = _session_scan_state.get(sid)
            if prev is not None and prev.get("stamp") != stamp:
                prev = None          # id reused after a reset — not the same chat
            due = (count > 0 and prev is not None
                   and prev.get("count") == count and not prev.get("done")) \
                if idle_only else (count > 0 and ((prev or {}).get("count") != count
                                                  or not (prev or {}).get("done")))
            if due:
                cwd = (s or {}).get("cwd")
                repo = _chat_repo_key(cwd) if cwd else None
                drained = False
                try:
                    # WALK the whole backlog on the daily pass. One fold only
                    # distils the turns that fit in AIFORGE_SESSION_COMPACT_CHARS
                    # and advances the offset by exactly those, so a day's chat
                    # needs several windows — folding once would leave the rest
                    # to a 30-min-idle daemon that no longer runs. Bounded so a
                    # runaway session can't hold the pass forever.
                    for _ in range(1 if idle_only else _max_windows):
                        r = chat_okr.compact_session(sid, repo=repo)
                        _af_log.info("session compact sid=%s: %s", sid, r)
                        r = r or {}
                        skipped = r.get("skipped")
                        if skipped in ("no_new", "too_short", "disabled"):
                            drained = True   # nothing left to fold for this session
                            break
                        if skipped:
                            # extract_failed / capture_failed / reset: the turns
                            # are still pending. This is what a provider outage
                            # looks like — the pass must NOT report success, or
                            # the whole retry budget never engages.
                            failed = failed or skipped in ("extract_failed",
                                                           "capture_failed")
                            break
                        if not r.get("ok"):
                            failed = True
                            break
                        if not r.get("remaining"):
                            drained = True   # backlog fully folded
                            break
                except Exception as exc:  # noqa: BLE001
                    _af_log.warning("session compact sid=%s failed: %s", sid, exc)
                    failed = True
                # done=False when the walk STOPPED SHORT (window cap, model
                # down, error): tomorrow's pass must revisit the session even if
                # no new message arrived, or the tail of a long day is folded by
                # nobody, ever.
                _session_scan_state[sid] = {"count": count, "stamp": stamp,
                                            "done": drained or idle_only}
            elif prev is None or prev.get("count") != count:
                _session_scan_state[sid] = {"count": count, "stamp": stamp,
                                            "done": False}
        live = {(s or {}).get("id") for s in sessions}
        for sid in list(_session_scan_state):
            if sid not in live:
                _session_scan_state.pop(sid, None)
        return not failed

    if _daily_hour is None:
        try:
            _idle_min = max(1, int(os.environ.get("AIFORGE_SESSION_IDLE_MIN", "30")))
        except (TypeError, ValueError):
            _idle_min = 30
        _pd.register("session-okr-compact", _compact_idle_sessions,
                     every_s=max(300, _idle_min * 60))
    else:
        # THE one evening pass. Order matters: sessions → captures first, then
        # captures → briefs, then the full re-fold of every brief, so a day's
        # chat reaches its brief in the SAME pass instead of waiting a day.
        # Which stages already succeeded TODAY. The pass raises so a failure
        # is retried — but the retry must not re-run the heavy stages that
        # worked (one broken session fold otherwise costs a second full
        # recompact), which the three separate tasks could never do.
        _pass_done: dict = {"day": None, "stages": set()}

        def _daily_compact() -> None:
            today = _date.today()
            if _pass_done["day"] != today:
                _pass_done.update(day=today, stages=set())
            ok = True
            # Each stage is isolated: as three separately registered tasks one
            # could not cancel the others, and folding them into one function
            # must not quietly reintroduce that coupling.
            for stage, run in (("sessions",
                                lambda: _compact_idle_sessions(idle_only=False)),
                               ("briefs", _compact_chat_md),
                               ("recompact", _recompact_all if _recompact_on
                                else lambda: True)):
                if stage in _pass_done["stages"]:
                    continue                 # already done today — skip on retry
                try:
                    if run():
                        _pass_done["stages"].add(stage)
                    else:
                        ok = False
                except Exception as exc:  # noqa: BLE001
                    _af_log.warning("daily compaction stage %s failed: %s",
                                    stage, exc)
                    ok = False
            if not ok:
                # RAISE so the scheduler retries (bounded) instead of counting a
                # pass that did nothing as today's compaction.
                raise RuntimeError("daily compaction pass failed — see warnings")

        # STRICT hour: the missed-slot catch-up must NOT drag this pass into
        # the working day. The whole point of the evening slot is that the
        # LLM-heavy fold happens when the operator is done — a laptop that was
        # asleep at 18:00 yesterday would otherwise start compacting at 09:00
        # the next morning, which is exactly the intrusion the schedule exists
        # to remove. It simply waits for today's 18:00 instead.
        # AIFORGE_COMPACT_CATCH_UP=1 restores the run-at-next-wake behaviour.
        _strict = os.environ.get("AIFORGE_COMPACT_CATCH_UP", "0") not in ("1", "true", "yes")
        _pd.register("daily-compact", _daily_compact, at_hour=_daily_hour,
                     strict_hour=_strict)
    _pd.start()


# ─────────────────────── API auth + bind-host guard ─────────────────────
# This control plane RUNS SHELL and EDITS FILES over HTTP, so exposing it
# unauthenticated is a remote-code-execution surface. Design (pragmatic, must
# not break local dev / the UI / the tests):
#   * AIFORGE_API_TOKEN set  → every /api/* route (except health) requires
#     EITHER a matching ``Authorization: Bearer <token>`` (or
#     ``X-AIForge-Token``) OR — only while AIFORGE_TRUST_LOOPBACK is on — a
#     loopback peer address. Loopback is trusted by default because reaching
#     the socket from this machine already implies read/write access to the
#     same files over the filesystem.
#   * THE ADMIN SURFACE (``/admin`` + ``/api/admin/*``) ALWAYS requires the
#     token when one is configured, loopback or not: it is the highest-value
#     screen and must not rest on the weakest signal we have.
#   * token unset → open (preserves local dev + the UI on localhost); a
#     non-loopback bind in that state is refused at boot instead.
#   * NON-loopback bind + no token → REFUSE TO BOOT (see _security_boot_guard).
# The UI static assets, ``/files`` and ``/`` stay open (no token) so the app
# shell can load; the browser then sends the operator-configured token on API
# calls. A single shared token — not user accounts. Keep it simple.


def _api_token() -> str:
    return os.environ.get("AIFORGE_API_TOKEN", "").strip()


def _sync_open() -> bool:
    """Whether the hub sync surface answers without a credential.

    **Open by default.** The admin's whole job is to receive every machine's
    memory and serve back what it distilled, and the deployment this was built
    for puts it on a trusted interface (a LAN or a WireGuard address) where the
    spokes need no secret to keep in step. ``AIFORGE_SYNC_AUTH=1`` closes it
    again, and then the ordinary API token is what a spoke must present.

    This is a *scoped* decision: it opens ``/api/memory/sync/*`` and nothing
    else. The control plane — which runs shells and writes config — still
    requires ``AIFORGE_API_TOKEN`` from every non-loopback caller, so an open
    sync surface never becomes an open shell.
    """
    return not _flag_on("AIFORGE_SYNC_AUTH", "0")


def _is_sync_path(path: str) -> bool:
    """The hub sync surface ``AIFORGE_SYNC_AUTH=0`` opens (and ONLY it).

    Matched on the raw request path, so a dot-segment or encoded-traversal
    variant (``/api/memory/sync/../chat/agent``) is rejected here rather than
    trusted to dead-end at the router: Starlette does not collapse ``..``, but a
    fronting proxy might, and an open sync path must never be a path that could
    dispatch to the control plane. The legitimate sync paths contain none of
    these, so refusing them costs nothing.
    """
    if not path.startswith("/api/memory/sync/"):
        return False
    lowered = path.lower()
    return not ("//" in path or ".." in path
                or "%2e" in lowered or "%2f" in lowered or "%5c" in lowered)


def _flag_on(name: str, default: str = "1") -> bool:
    return (os.environ.get(name) or default).strip().lower() \
        not in ("0", "false", "no", "off")


def _trust_loopback() -> bool:
    """Whether a loopback TCP peer counts as authenticated (AIFORGE_TRUST_LOOPBACK).

    Default ON so a bare local run keeps working with no configuration. It MUST
    be set to ``0`` on any deployment that is fronted by a reverse proxy on the
    same host (Cloudflare → nginx → this app is the documented one): the peer
    address the app sees is then the proxy's ``127.0.0.1`` for every request on
    earth, so implicit loopback trust becomes a full auth bypass. The trust is
    a deliberate configuration statement, never an accident of topology.
    """
    return _flag_on("AIFORGE_TRUST_LOOPBACK")


def _bind_host() -> str:
    """Pre-boot HINT for the host uvicorn binds to (AIFORGE_BIND_HOST, set by
    run.sh / docker-compose). Only a hint: the real listening address is read
    off the running server by ``_observed_bind_hosts`` — an env var says nothing
    about what a ``uvicorn --host 0.0.0.0`` actually did."""
    return (os.environ.get("AIFORGE_BIND_HOST") or "127.0.0.1").strip() or "127.0.0.1"


def _observed_bind_hosts() -> list[str]:
    """The addresses this process is REALLY listening on, or ``[]`` if unknown.

    ``AIFORGE_BIND_HOST`` is exported by run.sh only, so a systemd unit, a
    Dockerfile CMD or a developer typing ``uvicorn --host 0.0.0.0`` used to
    satisfy the boot guard with the loopback default while publishing a
    shell-running control plane to the LAN. So ask the server, not the env.

    Startup hooks run inside uvicorn's lifespan task, not under
    ``Server.startup``, so the server is not on our own stack — it is found by
    walking the coroutine frames of the live asyncio tasks. Real listening
    sockets win when they exist (``getsockname``); ``config.host`` is the
    answer during startup, before the sockets are created. Returns ``[]`` under
    TestClient / gunicorn / anything else we cannot introspect, which the
    caller must treat as "unobserved", not as "loopback".
    """
    try:
        import asyncio
        tasks = asyncio.all_tasks()
    except Exception:  # noqa: BLE001 — no running loop (TestClient, unit tests)
        return []
    server = None
    for task in tasks:
        coro = task.get_coro()
        while coro is not None and server is None:
            frame = getattr(coro, "cr_frame", None)
            if frame is None:
                break
            obj = frame.f_locals.get("self")
            cls = type(obj)
            if cls.__name__ == "Server" and cls.__module__.split(".")[0] == "uvicorn":
                server = obj
            coro = getattr(coro, "cr_await", None)
        if server is not None:
            break
    if server is None:
        return []
    hosts: list[str] = []
    for asgi_server in (getattr(server, "servers", None) or []):
        for sock in (getattr(asgi_server, "sockets", None) or []):
            try:
                hosts.append(str(sock.getsockname()[0]))
            except Exception:  # noqa: BLE001 — a unix socket has no host tuple
                continue
    if hosts:
        return hosts
    config = getattr(server, "config", None)
    if getattr(config, "uds", None) or getattr(config, "fd", None) is not None:
        return []                      # not an inet bind we can reason about
    host = getattr(config, "host", None)
    return [str(host)] if host else []


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


def _security_boot_guard(hosts: list[str] | None = None) -> None:
    """Refuse to boot when a shell-running control plane is listening on a
    non-loopback address without a token. Raises ``RuntimeError`` — called from
    a startup hook (where the REAL bind is observable) AND directly
    unit-testable by passing ``hosts``."""
    token = _api_token()
    boot_log = logging.getLogger("aiforge.boot")

    if _sync_open():
        # Not a refusal — it is the documented default (see ``_sync_open``) —
        # but the severity depends entirely on what this box is bound to, so the
        # line says which case it is rather than stating the setting and leaving
        # the operator to work it out.
        _bound = _bind_host()
        if _is_loopback_host(_bound):
            boot_log.info("memory sync is open (no credential) on "
                          "/api/memory/sync/* — reachable from this machine "
                          "only, since the bind host is %s.", _bound)
        else:
            boot_log.warning(
                "memory sync is OPEN (no credential) on /api/memory/sync/* AND "
                "bound to %s. Anything that can reach this port can WRITE "
                "memory that the merge folds into every machine's working "
                "knowledge. Keep this on a trusted interface (LAN/WireGuard), "
                "or set AIFORGE_SYNC_AUTH=1 here and AIFORGE_API_TOKEN on every "
                "machine.", _bound)

    observed = _observed_bind_hosts() if hosts is None else list(hosts)
    if observed:
        exposed = [h for h in observed if not _is_loopback_host(h)]
    else:
        # Nothing to observe (TestClient, gunicorn, an embedder): fall back to
        # the pre-boot hint and SAY SO, because the fallback is the thing that
        # used to be trusted silently.
        env_host = _bind_host()
        exposed = [] if _is_loopback_host(env_host) else [env_host]
        if not token:
            boot_log.warning(
                "could not observe the real listening address; falling back to "
                "AIFORGE_BIND_HOST=%s for the security guard — if this process "
                "actually binds a non-loopback address, set AIFORGE_API_TOKEN.",
                env_host)
    if not exposed:
        return
    where = ", ".join(exposed)
    # Escape hatch: the operator fronts the api with their OWN access layer
    # (Cloudflare Access / a WireGuard-only reverse proxy / nginx auth) and
    # accepts responsibility for exposure. Explicit opt-out so a bind to a
    # tunnel/LAN interface works without the app requiring a token.
    fronted = os.environ.get("AIFORGE_ALLOW_UNAUTH_NONLOOPBACK", "").strip().lower() \
        in ("1", "true", "yes", "on")
    if not token and not fronted:
        raise RuntimeError(
            f"AIForge refuses to boot: listening on a non-loopback host ({where}) "
            "exposes a shell-running control plane. Set AIFORGE_API_TOKEN to a "
            "shared secret (and configure the UI with it), bind 127.0.0.1, OR "
            "set AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1 if you front it yourself "
            "(Cloudflare / WireGuard-only proxy)."
        )
    if not token and fronted:
        boot_log.warning(
            "api listening on %s WITHOUT a token (AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1) "
            "— ensure your own access layer (Cloudflare/WireGuard/nginx) fronts it, "
            "and set AIFORGE_TRUST_LOOPBACK=0 so the proxy's loopback peer address "
            "does not read as authenticated.", where)
    elif token and _trust_loopback():
        boot_log.warning(
            "api listening on %s with AIFORGE_TRUST_LOOPBACK on — if a reverse "
            "proxy on THIS host forwards to it, every request arrives from "
            "127.0.0.1 and skips the token; set AIFORGE_TRUST_LOOPBACK=0.", where)


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


def _is_admin_path(path: str) -> bool:
    """The operator admin surface: the page and its data endpoint."""
    return path == "/admin" or path.startswith("/admin/") or path.startswith("/api/admin")


def _auth_exempt(path: str) -> bool:
    """Routes reachable without a token even when one is configured: health,
    the UI shell / static assets and the root redirect. Everything else under
    ``/api/`` is protected — as is the admin surface, which lives outside
    ``/api/`` but is never exempt."""
    if _is_admin_path(path):
        return False
    if path == "/api/health":
        return True
    # The hub sync surface, unless the operator closed it. Exempt rather than
    # "authenticated by a second credential": there is no mesh key any more, so
    # a spoke either needs the control-plane token (AIFORGE_SYNC_AUTH=1) or
    # nothing at all — and "nothing at all" is exactly an exemption.
    if _sync_open() and _is_sync_path(path):
        return True
    return not path.startswith("/api/")


def _request_is_loopback(request: Request) -> bool:
    """True when the request's TCP peer is this machine.

    Delegates to ``routes.admin._require_loopback`` — the admin page already
    owns this predicate, and a security check with two implementations WILL
    drift. That helper decides purely from ``request.client.host`` (the real
    peer address); X-Forwarded-For / X-Real-IP / Host / Forwarded are
    attacker-controlled and are deliberately never consulted. It raises
    ``HTTPException`` for "not local", which is adapted to a bool here.
    """
    from fastapi import HTTPException as _HTTPException
    try:
        _r_admin._require_loopback(request)
    except _HTTPException:
        return False
    return True


def _extract_request_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return (request.headers.get("x-aiforge-token", "") or "").strip()


@app.middleware("http")
async def _require_token(request: Request, call_next):
    token = _api_token()
    path = request.url.path
    # One credential now. The sync surface is either exempt (``_auth_exempt``,
    # the default) or held to the same API token as everything else.
    need_auth = bool(token)
    if (
        need_auth
        and request.method != "OPTIONS"          # let CORS preflight through
        and not _auth_exempt(path)
    ):
        supplied = _extract_request_token(request)
        ok_token = bool(supplied) and bool(token) and hmac.compare_digest(supplied, token)
        # Loopback may be trusted WITHOUT a token (AIFORGE_TRUST_LOOPBACK, on by
        # default): anyone who can reach the socket from this machine can
        # already read the memory tree (and everything else) straight off disk,
        # so a token adds nothing there. The token exists to authenticate
        # REMOTE callers. That trust is only sound when nothing on this host
        # forwards other people's requests into the socket — hence the flag.
        #
        # The admin surface follows the SAME rule rather than always demanding a
        # token. An earlier revision special-cased it, on the reasoning that the
        # highest-value surface should not rest on the weakest signal. The cost
        # was disproportionate: a browser navigation cannot send an
        # Authorization header, so the moment a token existed — which is the day
        # you add one remote peer — the local admin page stopped opening in a
        # browser at all. A fronted deployment must set AIFORGE_TRUST_LOOPBACK=0
        # for the rest of the API regardless, and that one flag closes the proxy
        # hole here too. The special case only helped when that flag was already
        # wrong, and it charged every correctly-configured operator for the
        # privilege.
        loopback_ok = _trust_loopback() and _request_is_loopback(request)
        if not ok_token and not loopback_ok:
            return JSONResponse(
                {"detail": "missing or invalid API token — this AIForge "
                           "requires AIFORGE_API_TOKEN for any caller that is "
                           "not a trusted loopback one. In the browser: "
                           "localStorage.setItem('aiforge_api_token', "
                           "'<token>') then reload."},
                status_code=401,
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


# Quiet the uvicorn ACCESS log for high-frequency polls: the /admin page hits
# /api/admin/sync-status every 10s and probes hit /api/health, so each would
# otherwise write an access line several times a minute, forever, burying the
# lines that matter. This filters ONLY those paths (and only the access log —
# errors and app logs are untouched); override the set with
# AIFORGE_ACCESS_LOG_MUTE (comma-separated substrings), or "" to mute nothing.
class _MuteHighFrequencyPolls(logging.Filter):
    """Drop uvicorn.access lines whose path matches any muted substring."""

    def __init__(self, muted: list[str]):
        super().__init__()
        self._muted = muted

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access passes (client, method, full_path, http_ver, status)
        # as record.args; fall back to the formatted message otherwise.
        try:
            path = str(record.args[2]) if record.args else record.getMessage()
        except (IndexError, TypeError):
            path = record.getMessage()
        return not any(m in path for m in self._muted)


def _install_access_log_filter() -> None:
    raw = os.environ.get("AIFORGE_ACCESS_LOG_MUTE",
                         "/api/admin/sync-status,/api/health")
    muted = [s.strip() for s in raw.split(",") if s.strip()]
    if not muted:
        return
    log = logging.getLogger("uvicorn.access")
    # Module-level class → isinstance matches across reloads, so a re-import
    # (tests, a hot reload) never stacks a second copy.
    if not any(isinstance(f, _MuteHighFrequencyPolls) for f in log.filters):
        log.addFilter(_MuteHighFrequencyPolls(muted))


_install_access_log_filter()


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
