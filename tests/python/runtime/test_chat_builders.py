"""Builder charters — one chat engine specialized per task by a prepended
charter banner (job|skill|workflow|rule), plus the create_job_script finalize
tool. No live LLM: complete_fn is stubbed to capture the system prompt."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime.prompts_extended import builders


def _capture(final="FINAL: done"):
    """Fake complete_fn that records the system prompt of the first call."""
    box: dict = {}

    def _fn(role, messages, **kw):
        box.setdefault("sys", messages[0]["content"])
        return final
    return box, _fn


# ── charter selection ────────────────────────────────────────────────────

def test_charter_for_each_builder():
    assert "JOB-BUILDER MODE" in builders.charter_for("job")
    assert "SKILL-BUILDER MODE" in builders.charter_for("skill")
    assert "WORKFLOW-BUILDER MODE" in builders.charter_for("workflow")
    assert "RULE-BUILDER MODE" in builders.charter_for("rule")
    assert set(builders.BUILDERS) == {"job", "skill", "workflow", "rule"}


def test_charter_for_unknown_or_empty_is_none():
    assert builders.charter_for("bogus") is None
    assert builders.charter_for(None) is None
    assert builders.charter_for("JOB") is not None      # case-insensitive


# ── charter injection into the system prompt ─────────────────────────────

def test_builder_injects_charter_into_system_prompt(tmp_path):
    box, fn = _capture()
    list(ca.run_chat_agent([{"role": "user", "content": "every day pull repos"}],
                           cwd=str(tmp_path), complete_fn=fn, builder="job"))
    assert "JOB-BUILDER MODE" in box["sys"]
    assert "create_job_script" in box["sys"]            # finalize tool advertised


def test_no_builder_means_no_charter(tmp_path):
    box, fn = _capture()
    list(ca.run_chat_agent([{"role": "user", "content": "hi"}],
                           cwd=str(tmp_path), complete_fn=fn))
    assert "JOB-BUILDER MODE" not in box["sys"]


def test_bad_builder_name_does_not_crash(tmp_path):
    box, fn = _capture()
    evs = list(ca.run_chat_agent([{"role": "user", "content": "hi"}],
                                 cwd=str(tmp_path), complete_fn=fn,
                                 builder="nonsense"))
    assert evs[-1]["type"] == "done"
    assert "JOB-BUILDER MODE" not in box["sys"]


# ── create_job_script finalize tool ──────────────────────────────────────

@pytest.fixture
def _jobs_env(monkeypatch, tmp_path):
    from aiforge_core.jobs import store
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))
    store.reset_backend_for_tests()
    yield
    store.reset_backend_for_tests()


def test_create_job_script_tool_writes_script_and_schedules(_jobs_env, tmp_path):
    import os

    from aiforge_core.jobs import scripts, store
    res = ca._t_create_job_script(
        {"name": "nightly cleanup", "cron": "0 9 * * *",
         "script": "echo hi"}, str(tmp_path))
    assert res["ok"] is True
    assert os.path.isfile(res["script_path"])
    assert scripts.is_within_jobs_dir(res["script_path"])
    job = store.get(res["job_id"])
    assert job["kind"] == "script"
    assert job["script_path"] == res["script_path"]


def test_create_job_script_tool_rejects_bad_cron(_jobs_env, tmp_path):
    res = ca._t_create_job_script(
        {"name": "x", "cron": "not-a-cron", "script": "true"}, str(tmp_path))
    assert res["ok"] is False
    assert "cron" in res["error"]


def test_create_job_script_tool_needs_all_fields(_jobs_env, tmp_path):
    res = ca._t_create_job_script({"name": "x", "cron": "0 9 * * *"},
                                  str(tmp_path))
    assert res["ok"] is False


def test_create_job_script_registered_in_tools():
    assert ca.TOOLS.get("create_job_script") is ca._t_create_job_script
