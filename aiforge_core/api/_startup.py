"""What the API does when it starts: config and CA hygiene, runtime env,
model context, tool parity, agent reassignment, memory migrations, the jobs
scheduler and the daily reindex. api.py registers these in this order."""
from __future__ import annotations

import logging
import os

from aiforge_core.api.routes import agents as _r_agents

from ._compaction_jobs import (
    _compact_at_hour,
    _register_artifact_merge,
    _register_daily_compaction,
    _register_hourly_jobs,
    _register_idle_compaction,
    _register_legacy_compaction,
)


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.api.api as package
    return package


def _repair_config_permissions() -> None:
    """Tighten an existing config dir before anything else reads it.

    ``_atomic`` writes new files at 0600, but that never applied to the files
    already on disk — ``agent_config.json`` was found holding a live api_key at
    0644 long after that hardening landed, because nothing had rewritten it.
    Runs first so a token is not world-readable for the length of a boot."""
    try:
        # Consolidate first: the credential files move into security/ (0700),
        # and the repair below then tightens whatever is left in the root.
        # Order matters — repairing a path we are about to move is wasted work,
        # and moving after the repair leaves a boot's worth of exposure.
        from aiforge_core.config import secure_store
        secure_store.migrate_all()
    except Exception as exc:  # noqa: BLE001 — never block boot on this
        logging.getLogger("aiforge.secure_store").warning(
            "credential consolidation skipped: %s", exc)
    try:
        from aiforge_core.config import permissions
        permissions.repair()
    except Exception as exc:  # noqa: BLE001 — never block boot on this
        logging.getLogger("aiforge.permissions").warning(
            "permission repair skipped: %s", exc)


def _publish_ca_bundle() -> None:
    """Put the estate's CA into this process's environment before anything
    spawns a subprocess.

    An internal CA used to reach the model client and the integration helpers
    and stop there, so ``git clone`` against an internal GitLab failed with a
    certificate error while the REST calls to the same host worked. Publishing
    it here means git, gh, curl, npm and anything the agent runs in its shell
    inherit the same trust, and an operator who already set one of those
    variables keeps their value."""
    try:
        from aiforge_core.net import ca
        ca.apply_to_process_env()
    except Exception as exc:  # noqa: BLE001 — never block boot on this
        logging.getLogger("aiforge.ca").warning("CA bundle not applied: %s", exc)


def _guard_and_announce_backends() -> None:
    """FIRST boot step: in data-driven mode (AIFORGE_REQUIRE_DATA_BACKEND=1)
    abort LOUD if any data store still resolves to embedded SQLite, then log
    one line naming every backend. The guard is intentionally hard-fail; the
    log is soft (never crashes boot)."""
    from aiforge_core.config import backends
    backends.require_data_backends()   # no-op (SQLite-only build)
    backends.boot_log()                # soft one-line announcement


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


def _apply_runtime_env_line(line: str, keep_pg: bool) -> None:
    """Apply one ``KEY=VALUE`` line from runtime.env into os.environ. A real env
    var / project .env already set WINS (never clobbered); comments/blanks and
    stale Postgres/Neo4j keys (single mode is SQLite) are ignored."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return
    k, _, v = line.partition("=")
    k = k.strip()
    if k in _RUNTIME_ENV_DB_KEYS and not keep_pg:
        return                                # SQLite-only; ignore stale DB pointers
    if k and k not in os.environ:             # don't clobber real env/.env
        os.environ[k] = v.strip()


def _load_runtime_env() -> None:
    """Restore UI-persisted toggles (runtime.env) into the process env on boot
    using a plain KEY=VALUE parser — NOT a shell source — so a value can never
    be executed. A real env var / project .env already in the environment WINS
    (setdefault), keeping them the operator's explicit escape hatch. Stale
    Postgres/Neo4j backend keys are SKIPPED (single mode is SQLite)."""
    try:
        from aiforge_core.api._shared import _RUNTIME_ENV_PATH
        if not os.path.isfile(_RUNTIME_ENV_PATH):
            return
        keep_pg = os.environ.get("AIFORGE_KEEP_PG") == "1"
        with open(_RUNTIME_ENV_PATH) as f:
            for raw in f:
                _apply_runtime_env_line(raw, keep_pg)
    except Exception:  # noqa: BLE001
        pass


def _models_below_context(want: int) -> list[str]:
    """LM Studio's loaded model ids whose context is under ``want``. Queries the
    local /api/v0/models endpoint; raises on any network/parse error (caller
    swallows it)."""
    import json as _j
    import urllib.request as _u
    base = os.environ.get("AIFORGE_LM_BASE_URL",
                          "http://127.0.0.1:1234/v1").rstrip("/")
    api0 = base.rsplit("/v1", 1)[0] + "/api/v0/models"
    data = _j.loads(_u.urlopen(api0, timeout=8).read())
    return [mid for m in data.get("data", [])
            if m.get("state") == "loaded"
            and (mid := m.get("id"))
            and (m.get("loaded_context_length") or 0) < want]


def _reload_models_to_context(below: list[str], want: int) -> None:
    """Reload each named model at the ``want`` context. Best-effort per model."""
    from aiforge_core.runtime import local_starter
    for mid in below:
        try:
            local_starter.load_model_now(mid, want, ttl=43200)
            _pkg()._af_log.info("boot ctx-reload: %s -> %d", mid, want)
        except Exception as _e:  # noqa: BLE001
            _pkg()._af_log.debug("boot ctx-reload failed for %s: %s", mid, _e)


def _recover_interrupted_turns() -> None:
    """A chat turn that was running when the server died becomes a stopped
    turn, so Retry resumes it instead of the work being lost."""
    try:
        from aiforge_core.runtime import chat_turn_save
        n = chat_turn_save.recover_all()
        if n:
            logging.getLogger("aiforge").info("recovered %d interrupted chat turn(s)", n)
    except Exception as exc:  # noqa: BLE001 — never block boot on this
        logging.getLogger("aiforge").warning("turn recovery skipped: %s", exc)


def _ensure_model_context_on_boot() -> None:
    """Post-deploy, LM Studio JIT-loads the local model at its small default
    context (e.g. 8192), which HTTP-400s the big prompts a multi-file build needs
    — the recurring `llm.exhausted`. On boot, in a background thread, query the
    loaded model(s) and reload any below the target context. Model-agnostic;
    best-effort; AIFORGE_NO_CTX_RELOAD=1 skips, AIFORGE_LM_CONTEXT sets target."""
    if os.environ.get("AIFORGE_NO_CTX_RELOAD"):
        return

    def _work():
        try:
            import time as _t
            _t.sleep(8)                       # let the server + LM Studio settle
            try:
                want = int(os.environ.get("AIFORGE_LM_CONTEXT", "262144"))
            except ValueError:
                want = 262144
            below = _models_below_context(want)
            if below:
                _pkg()._reload_models_to_context(below, want)
        except Exception as _exc:  # noqa: BLE001 — never break boot
            _pkg()._af_log.debug("boot ctx-reload skipped: %s", _exc)

    _pkg()._spawn(_work, name="ctx-reload")


def _check_tool_parity() -> None:
    """Warn (loudly, on the box) if a cross-surface tool drifted between the
    chat + Doer registries — the recurring 'works in chat, not in pipeline' bug.
    Startup check, not just CI. Never blocks startup."""
    try:
        from aiforge_core.runtime import tool_manifest
        tool_manifest.validate_or_warn()
    except Exception:  # noqa: BLE001
        pass


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


def _run_memory_migrations() -> None:
    """Auto-upgrade EVERY deployment's memory into the current scoped-OKR shape:
    legacy brief format → OKR envelope, compacted briefs → OKR learnings, then
    flat okr/ → global/ + projects/<repo>/. Idempotent (one-shot steps are
    marker-guarded); never blocks startup. This is the migration path
    new/upgrading users get for free on ``run.sh`` (which boots this API)."""
    def _run():
        try:
            from aiforge_core.memory import migrations
            r = migrations.run_startup_migrations()
            _pkg()._af_log.info("memory migrations: %s",
                         {k: (v.get("moved") or v.get("migrated")
                              or v.get("skipped") or v.get("ok"))
                          for k, v in r.items()})
        except Exception:  # noqa: BLE001 — migration is best-effort
            pass
    # background thread: the classify step calls the LLM, which must not delay
    # the API coming up. Migrations are idempotent + marker-guarded.
    try:
        _pkg()._spawn(_run, name="memory-migrations")
    except Exception:  # noqa: BLE001
        _run()


def _start_jobs_scheduler() -> None:
    """Scheduled-jobs tick loop — daemon thread, same pattern as the
    other background workers. AIFORGE_JOBS_DISABLE=1 skips it.

    Its registration was STOLEN on 2026-07-07: a refactor inserted
    `_check_tool_parity` directly beneath it and took the registration with it,
    leaving `@app.on_event("startup")` written twice on that function and none
    on this one. Every scheduled job since has sat in the table with a
    next_run_at that nothing advanced — rows written, tickets never filed, no
    error anywhere. Only POST /api/jobs/{id}/run-now did anything.
    It is now registered in api.py's startup tuple, and
    tests/python/api/test_jobs_scheduler_started.py asserts the registration,
    because nothing else would notice it disappearing again."""
    try:
        from aiforge_core.runtime import bg_work
        bg_work.resume_after_restart()
    except Exception:  # noqa: BLE001 — startup must never crash the API
        pass
    try:
        from aiforge_core.jobs import scheduler as jobs_scheduler
        if jobs_scheduler._disabled():
            return
        _pkg()._spawn(jobs_scheduler.run_loop, name="jobs-scheduler")
    except Exception:  # noqa: BLE001 — startup must never crash the API
        pass


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
    _register_hourly_jobs(_pd, hour)
    _register_artifact_merge(_pd)
    # Compaction is ENABLED BY DEFAULT (Option A): the per-category rate limiter
    # meters it as compaction (the remainder of llm_max_rpm), so it can no longer spend a
    # burst of requests before the app is usable. Turn it OFF from Settings
    # (persists AIFORGE_COMPACT_DISABLE=1). One source of truth for the flag:
    # compact_window.disabled(). Reindex + hourly jobs run regardless.
    from aiforge_core.runtime import compact_window as _cw
    if not _cw.disabled():
        daily_hour = _compact_at_hour()
        if _cw.idle_mode():
            _register_idle_compaction(_pd)
        elif daily_hour is None:
            _register_legacy_compaction(_pd)
        else:
            _register_daily_compaction(_pd, daily_hour)
    _pd.start()
