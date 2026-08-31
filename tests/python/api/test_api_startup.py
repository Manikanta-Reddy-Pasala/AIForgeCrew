"""API boot: runtime.env restore, model context, migrations, schedulers.

runtime.env is parsed with a plain KEY=VALUE reader, never shell-sourced, so a
persisted value can't execute; a real env var already set WINS, keeping it the
operator's escape hatch; and stale Postgres/Neo4j pointers from an older hybrid
setup are SKIPPED, because restoring them would make tickets, chat and memory
all try a database that no longer exists.

Two boot steps exist because of specific field failures. LM Studio JIT-loads a
model at its small default context, which HTTP-400s the big prompts a
multi-file build needs, so any loaded model below the target is reloaded in the
background. And the jobs scheduler's startup registration was once silently
stolen by a refactor — every scheduled job then sat with a next_run_at nothing
advanced, no error anywhere.
"""
from __future__ import annotations

import os

import pytest

from aiforge_core.api import api as api_mod


# ─── runtime.env restore ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("AIFORGE_TEST_KNOB", "AIFORGE_PG_URL", "AIFORGE_KEEP_PG",
                "AIFORGE_NEO4J_URI", "AIFORGE_MEMORY_BACKEND",
                "AIFORGE_NO_CTX_RELOAD", "AIFORGE_LM_CONTEXT",
                "AIFORGE_LM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def test_a_persisted_toggle_is_restored():
    api_mod._apply_runtime_env_line("AIFORGE_TEST_KNOB=on", keep_pg=False)
    assert os.environ["AIFORGE_TEST_KNOB"] == "on"
    os.environ.pop("AIFORGE_TEST_KNOB")


def test_a_real_env_var_is_never_clobbered(monkeypatch):
    """The operator's explicit escape hatch."""
    monkeypatch.setenv("AIFORGE_TEST_KNOB", "from-shell")
    api_mod._apply_runtime_env_line("AIFORGE_TEST_KNOB=from-file", keep_pg=False)
    assert os.environ["AIFORGE_TEST_KNOB"] == "from-shell"


@pytest.mark.parametrize("line", ["", "   ", "# a comment", "no equals sign"])
def test_comments_and_junk_are_ignored(line):
    api_mod._apply_runtime_env_line(line, keep_pg=False)


@pytest.mark.parametrize("key", sorted(api_mod._RUNTIME_ENV_DB_KEYS))
def test_a_stale_database_pointer_is_never_restored(key):
    """This build is SQLite-only — restoring these makes tickets, chat and
    memory all try a Postgres/Neo4j that no longer exists."""
    api_mod._apply_runtime_env_line(f"{key}=postgres://old", keep_pg=False)
    assert key not in os.environ


def test_a_real_external_postgres_can_still_be_kept():
    api_mod._apply_runtime_env_line("AIFORGE_PG_URL=postgres://real", keep_pg=True)
    assert os.environ["AIFORGE_PG_URL"] == "postgres://real"
    os.environ.pop("AIFORGE_PG_URL")


def test_the_whole_file_is_read_on_boot(monkeypatch, tmp_path):
    env = tmp_path / "runtime.env"
    env.write_text("# header\nAIFORGE_TEST_KNOB=on\nAIFORGE_PG_URL=postgres://x\n")
    import aiforge_core.api._shared as shared
    monkeypatch.setattr(shared, "_RUNTIME_ENV_PATH", str(env))
    api_mod._load_runtime_env()
    assert os.environ["AIFORGE_TEST_KNOB"] == "on"
    assert "AIFORGE_PG_URL" not in os.environ
    os.environ.pop("AIFORGE_TEST_KNOB")


def test_a_missing_runtime_env_is_fine(monkeypatch, tmp_path):
    import aiforge_core.api._shared as shared
    monkeypatch.setattr(shared, "_RUNTIME_ENV_PATH", str(tmp_path / "gone.env"))
    api_mod._load_runtime_env()


def test_an_unreadable_runtime_env_never_blocks_boot(monkeypatch, tmp_path):
    env = tmp_path / "runtime.env"
    env.write_text("AIFORGE_TEST_KNOB=on\n")
    import aiforge_core.api._shared as shared
    monkeypatch.setattr(shared, "_RUNTIME_ENV_PATH", str(env))
    monkeypatch.setattr("builtins.open",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
    api_mod._load_runtime_env()


# ─── the model context reload ──────────────────────────────────────────


@pytest.fixture()
def lm_studio(monkeypatch):
    import urllib.request as u
    state: dict = {"payload": {"data": [
        {"id": "small", "state": "loaded", "loaded_context_length": 8192},
        {"id": "big", "state": "loaded", "loaded_context_length": 262144},
        {"id": "unloaded", "state": "not-loaded", "loaded_context_length": 1},
    ]}, "url": None}

    class _Resp:
        def read(self):
            import json
            return json.dumps(state["payload"]).encode()

    def _urlopen(url, timeout=None):
        state["url"] = url
        if isinstance(state["payload"], Exception):
            raise state["payload"]
        return _Resp()
    monkeypatch.setattr(u, "urlopen", _urlopen)
    return state


def test_only_loaded_models_below_the_target_are_returned(lm_studio):
    assert api_mod._models_below_context(262144) == ["small"]
    assert lm_studio["url"].endswith("/api/v0/models")


def test_the_probe_follows_the_configured_endpoint(lm_studio, monkeypatch):
    monkeypatch.setenv("AIFORGE_LM_BASE_URL", "http://box:9999/v1")
    api_mod._models_below_context(1000)
    assert lm_studio["url"].startswith("http://box:9999/api/v0/models")


def test_each_undersized_model_is_reloaded(monkeypatch):
    from aiforge_core.runtime import local_starter
    seen: list = []
    monkeypatch.setattr(local_starter, "load_model_now",
                        lambda mid, want, ttl=0: seen.append((mid, want, ttl)))
    api_mod._reload_models_to_context(["small", "other"], 262144)
    assert seen == [("small", 262144, 43200), ("other", 262144, 43200)]


def test_one_failed_reload_does_not_stop_the_rest(monkeypatch):
    from aiforge_core.runtime import local_starter
    seen: list = []

    def _load(mid, want, ttl=0):
        seen.append(mid)
        if mid == "small":
            raise RuntimeError("ssh refused")
    monkeypatch.setattr(local_starter, "load_model_now", _load)
    api_mod._reload_models_to_context(["small", "other"], 1000)
    assert seen == ["small", "other"]


def test_the_boot_reload_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_NO_CTX_RELOAD", "1")
    monkeypatch.setattr(api_mod, "_spawn",
                        lambda fn, name=None: pytest.fail("spawned with the gate off"))
    api_mod._ensure_model_context_on_boot()


def test_the_boot_reload_runs_in_the_background(monkeypatch, lm_studio):
    """It sleeps to let LM Studio settle — never on the boot path."""
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    reloaded: list = []
    monkeypatch.setattr(api_mod, "_reload_models_to_context",
                        lambda below, want: reloaded.append((below, want)))
    monkeypatch.setattr(api_mod, "_spawn", lambda fn, name=None: fn())
    api_mod._ensure_model_context_on_boot()
    assert reloaded == [(["small"], 262144)]


def test_a_junk_context_target_falls_back(monkeypatch, lm_studio):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setenv("AIFORGE_LM_CONTEXT", "huge")
    reloaded: list = []
    monkeypatch.setattr(api_mod, "_reload_models_to_context",
                        lambda below, want: reloaded.append(want))
    monkeypatch.setattr(api_mod, "_spawn", lambda fn, name=None: fn())
    api_mod._ensure_model_context_on_boot()
    assert reloaded == [262144]


def test_an_unreachable_lm_studio_never_breaks_boot(monkeypatch, lm_studio):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    lm_studio["payload"] = OSError("connection refused")
    monkeypatch.setattr(api_mod, "_spawn", lambda fn, name=None: fn())
    api_mod._ensure_model_context_on_boot()


def test_nothing_to_reload_is_a_no_op(monkeypatch, lm_studio):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    lm_studio["payload"] = {"data": []}
    monkeypatch.setattr(api_mod, "_reload_models_to_context",
                        lambda *a: pytest.fail("reloaded with nothing undersized"))
    monkeypatch.setattr(api_mod, "_spawn", lambda fn, name=None: fn())
    api_mod._ensure_model_context_on_boot()


# ─── the other boot steps ──────────────────────────────────────────────


def test_the_backend_guard_runs_before_the_announcement(monkeypatch):
    from aiforge_core.config import backends
    calls: list = []
    monkeypatch.setattr(backends, "require_data_backends",
                        lambda: calls.append("guard"))
    monkeypatch.setattr(backends, "boot_log", lambda: calls.append("log"))
    api_mod._guard_and_announce_backends()
    assert calls == ["guard", "log"]


def test_the_playbook_dirs_are_created(monkeypatch):
    from aiforge_core.runtime import workflows
    called: list = []
    monkeypatch.setattr(workflows, "ensure_dirs", lambda: called.append(1))
    api_mod._ensure_skill_workflow_dirs()
    assert called == [1]


def test_a_failed_dir_create_never_blocks_boot(monkeypatch):
    from aiforge_core.runtime import workflows
    monkeypatch.setattr(workflows, "ensure_dirs",
                        lambda: (_ for _ in ()).throw(OSError("read-only")))
    api_mod._ensure_skill_workflow_dirs()


def test_tool_parity_is_checked_on_boot_not_only_in_ci(monkeypatch):
    """The recurring "works in chat, not in pipeline" drift."""
    from aiforge_core.runtime import tool_manifest
    called: list = []
    monkeypatch.setattr(tool_manifest, "validate_or_warn",
                        lambda: called.append(1))
    api_mod._check_tool_parity()
    assert called == [1]


def test_a_failed_parity_check_never_blocks_boot(monkeypatch):
    from aiforge_core.runtime import tool_manifest
    monkeypatch.setattr(tool_manifest, "validate_or_warn",
                        lambda: (_ for _ in ()).throw(RuntimeError("bad manifest")))
    api_mod._check_tool_parity()


def test_agents_are_reassigned_on_boot(monkeypatch):
    """So an EXISTING config picks up mapping fixes — e.g. quick roles moving
    off a reasoning model that returns empty."""
    called: list = []
    monkeypatch.setattr(api_mod._r_agents, "_reassign_by_capability",
                        lambda: called.append(1))
    api_mod._reassign_agents_on_boot()
    assert called == [1]


def test_a_failed_reassignment_never_blocks_boot(monkeypatch):
    monkeypatch.setattr(api_mod._r_agents, "_reassign_by_capability",
                        lambda: (_ for _ in ()).throw(RuntimeError("no models")))
    api_mod._reassign_agents_on_boot()


def test_memory_migrations_run_off_the_boot_path(monkeypatch):
    """The classify step calls the LLM, which must not delay the API coming
    up."""
    from aiforge_core.memory import migrations
    seen: dict = {}
    monkeypatch.setattr(migrations, "run_startup_migrations",
                        lambda: seen.setdefault("ran", True) and {"format": {"ok": True}})
    monkeypatch.setattr(api_mod, "_spawn",
                        lambda fn, name=None: seen.update(name=name) or fn())
    api_mod._run_memory_migrations()
    assert seen["ran"] is True and seen["name"] == "memory-migrations"


def test_migrations_still_run_when_the_thread_cannot_start(monkeypatch):
    from aiforge_core.memory import migrations
    ran: list = []
    monkeypatch.setattr(migrations, "run_startup_migrations",
                        lambda: ran.append(1) or {})
    monkeypatch.setattr(api_mod, "_spawn",
                        lambda fn, name=None: (_ for _ in ()).throw(
                            RuntimeError("no threads")))
    api_mod._run_memory_migrations()
    assert ran == [1]


def test_a_failed_migration_never_blocks_boot(monkeypatch):
    from aiforge_core.memory import migrations
    monkeypatch.setattr(migrations, "run_startup_migrations",
                        lambda: (_ for _ in ()).throw(RuntimeError("bad tree")))
    monkeypatch.setattr(api_mod, "_spawn", lambda fn, name=None: fn())
    api_mod._run_memory_migrations()


def test_the_jobs_scheduler_loop_is_started(monkeypatch):
    """Its registration was once stolen by a refactor: every scheduled job then
    sat with a next_run_at nothing advanced, with no error anywhere."""
    from aiforge_core.jobs import scheduler
    seen: dict = {}
    monkeypatch.setattr(scheduler, "_disabled", lambda: False)
    monkeypatch.setattr(api_mod, "_spawn",
                        lambda fn, name=None: seen.update(fn=fn, name=name))
    api_mod._start_jobs_scheduler()
    assert seen["name"] == "jobs-scheduler" and seen["fn"] is scheduler.run_loop


def test_the_scheduler_can_be_disabled(monkeypatch):
    from aiforge_core.jobs import scheduler
    monkeypatch.setattr(scheduler, "_disabled", lambda: True)
    monkeypatch.setattr(api_mod, "_spawn",
                        lambda fn, name=None: pytest.fail("started while disabled"))
    api_mod._start_jobs_scheduler()


def test_a_failed_scheduler_start_never_crashes_the_api(monkeypatch):
    from aiforge_core.jobs import scheduler
    monkeypatch.setattr(scheduler, "_disabled",
                        lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    api_mod._start_jobs_scheduler()
