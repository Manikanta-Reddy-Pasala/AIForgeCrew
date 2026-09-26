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

import hmac
import logging
import os
from datetime import date as _date  # noqa: F401  # the jobs look it up here

from fastapi import (
    FastAPI,
    HTTPException,  # noqa: F401  # tests use api.HTTPException
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aiforge_core.runtime.background import (
    spawn as _spawn,  # noqa: F401  # the jobs look it up here
)
from aiforge_core.tickets import store as tickets_mod  # noqa: F401  # api.tickets_mod

from ._bind_security import (  # noqa: F401  # used here or by tests
    _api_token,
    _auth_exempt,
    _extract_request_token,
    _is_sync_path,
    _observed_bind_hosts,
    _request_is_loopback,
    _security_boot_guard,
    _sync_open,
    _trust_loopback,
)
from ._compaction_jobs import (  # noqa: F401  # used here or by tests
    _compact_at_hour,
    _register_artifact_merge,
    _register_daily_compaction,
    _register_hourly_jobs,
    _register_idle_compaction,
    _register_legacy_compaction,
)
from ._startup import (  # noqa: F401  # re-exported
    _RUNTIME_ENV_DB_KEYS,
    _apply_runtime_env_line,
    _check_tool_parity,
    _ensure_model_context_on_boot,
    _ensure_skill_workflow_dirs,
    _guard_and_announce_backends,
    _load_runtime_env,
    _models_below_context,
    _publish_ca_bundle,
    _reassign_agents_on_boot,
    _recover_interrupted_turns,
    _reload_models_to_context,
    _repair_config_permissions,
    _run_memory_migrations,
    _start_daily_reindex,
    _start_jobs_scheduler,
)
from ._ui_serving import (  # noqa: F401  # used here or by tests
    _DIST,
    _INDEX_HTML,
    _cors_origins,
    _install_access_log_filter,
    _MuteHighFrequencyPolls,
    _resolve_dist,
)

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
from aiforge_core.api.routes import admin as _r_admin  # noqa: E402
from aiforge_core.api.routes import agents as _r_agents  # noqa: E402
from aiforge_core.api.routes import chat as _r_chat  # noqa: E402
from aiforge_core.api.routes import files as _r_files  # noqa: E402
from aiforge_core.api.routes import groups as _r_groups  # noqa: E402
from aiforge_core.api.routes import integrations as _r_integrations  # noqa: E402
from aiforge_core.api.routes import jobs as _r_jobs  # noqa: E402
from aiforge_core.api.routes import library as _r_library  # noqa: E402
from aiforge_core.api.routes import mcp as _r_mcp  # noqa: E402
from aiforge_core.api.routes import memory as _r_memory  # noqa: E402
from aiforge_core.api.routes import observability as _r_observability  # noqa: E402
from aiforge_core.api.routes import repos as _r_repos  # noqa: E402
from aiforge_core.api.routes import rules as _r_rules  # noqa: E402
from aiforge_core.api.routes import runtime as _r_runtime  # noqa: E402
from aiforge_core.api.routes import sync as _r_sync  # noqa: E402
from aiforge_core.api.routes import tickets as _r_tickets  # noqa: E402

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
app.include_router(_r_groups.router)
app.include_router(_r_admin.router)

# Startup work lives in _startup; it runs in this order, before the bind
# security check registered further down.
for _startup_step in (
        _repair_config_permissions,
        _publish_ca_bundle,
        _guard_and_announce_backends,
        _ensure_skill_workflow_dirs,
        _load_runtime_env,
        _recover_interrupted_turns,
        _ensure_model_context_on_boot,
        _check_tool_parity,
        _reassign_agents_on_boot,
        _run_memory_migrations,
        _start_jobs_scheduler,
        _start_daily_reindex,
):
    # app.router: Starlette 1.x dropped app.add_event_handler; this is what
    # the @app.on_event decorator calls.
    app.router.add_event_handler("startup", _startup_step)

# Process exit ends every wait for the model (llm/model_wait) instead of
# leaving worker threads blocked on an endpoint that is down.
from aiforge_core.llm import model_wait as _model_wait  # noqa: E402
app.router.add_event_handler("shutdown", _model_wait.shutdown)

# Backwards-compat re-exports: private chat helpers relocated into
# aiforge_core.api.routes.chat but still imported by name from
# aiforge_core.api.api (tests). Keep them reachable at the old path.
from aiforge_core.api.routes.chat import (  # noqa: E402,F401
    _chat_history_for_agent,
    _delete_chat_workspace,
    _step_digest,
)
from aiforge_core.api.routes.files import serve_ticket_file  # noqa: E402,F401

# Ticket + file helpers/models relocated into their route modules but still
# referenced by name from aiforge_core.api.api (tests). Keep them reachable.
from aiforge_core.api.routes.tickets import (  # noqa: E402,F401
    AttachedFile,
    _persist_ticket_attachments,
    _remove_ticket_attachments,
)

# agent_config re-export — the agents/chat config surface moved to route
# modules, but tests still reach it as aiforge_core.api.api._acfg.
from aiforge_core.config import agent_config as _acfg  # noqa: E402,F401

# Session-end OKR compaction — IDLE trigger. AIFORGE_SESSION_COMPACT selects
# the trigger (idle | turns | explicit | off); the daemon only runs the idle
# scan. Idle is detected without parsing message timestamps: a session whose
# message count is UNCHANGED across two consecutive scans (spaced
# AIFORGE_SESSION_IDLE_MIN apart) has gone quiet → compact it once. State is
# in-process (resets on restart, which is fine — an active session just waits
# one more idle window).
_SESSION_SCAN_STATE: dict = {}


# Which stages of the evening pass already succeeded TODAY. The pass raises so
# a failure is retried — but the retry must not re-run the heavy stages that
# worked (one broken session fold otherwise costs a second full recompact),
# which three separately registered tasks could never do.
_PASS_DONE: dict = {"day": None, "stages": set()}


@app.on_event("startup")
def _enforce_bind_security() -> None:
    _security_boot_guard()


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


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


_install_access_log_filter()


# ─────────────────────────── Boot-time wiring ───────────────────────────
# OpenTelemetry — no-op when AIFORGE_OTEL_ENABLED != "1" (see otel.py).
# Initialised once at module load so every request inherits the tracer.
try:
    from aiforge_core.observability import otel as _otel
    _otel.setup()
except Exception as _exc:
    print(f"[boot] otel setup skipped: {_exc}")

if os.path.isdir(_DIST):
    # SPA fallback: any unknown path under /ui/ returns index.html so
    # react-router can handle the route client-side.
    class _SpaStatic(StaticFiles):
        async def get_response(self, path: str, scope):
            try:
                resp = await super().get_response(path, scope)
            except Exception:
                resp = FileResponse(os.path.join(_DIST, _INDEX_HTML))
            # index.html / the SPA shell must never be cached, or a deploy
            # leaves users on a stale bundle that references deleted asset
            # hashes ("everything broken" after an update). The hashed
            # assets under /ui/assets/ stay cacheable.
            if path in ("", "/", _INDEX_HTML) or not path.startswith("assets/"):
                if getattr(resp, "media_type", "") == "text/html" or path in ("", "/", _INDEX_HTML):
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
