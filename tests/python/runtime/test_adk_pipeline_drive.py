"""Driving an ADK run: deadlines, call caps, and partial-state recovery.

A run that trips its LLM-call ceiling or its wall-clock deadline must not
crash the runner. It recovers whatever the session accumulated, tags WHY it
stopped, and — for the pipeline — marks the verdict failed so the ticket lands
as blocked with its partial state rather than hanging.

The post-PR live verifier is the other shape here: a standalone single-agent
run seeded with the PR url and a diff stat, so it knows what changed without
inheriting the whole pipeline history (which overflowed the model). PR_URL is
exported for the recipe's bash and restored afterwards.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import types as pytypes

import pytest

from aiforge_core.runtime.adk_runner import _pipeline as pl


class _Session:
    def __init__(self, state=None):
        self.id = "sess-1"
        self.state = state or {}
        self.events: list = []


class _SessionSvc:
    def __init__(self, state=None):
        self._session = _Session(state)
        self.created: dict = {}

    async def create_session(self, app_name=None, user_id=None, state=None):
        # Record what was seeded; the run's FINAL state is whatever the
        # fixture preloaded (as if the agents had written it).
        self.created = {"state": state}
        return self._session

    async def get_session(self, app_name=None, user_id=None, session_id=None):
        return self._session


class _Runner:
    """A runner whose run_async either drains events or raises."""

    def __init__(self, error=None, mutate=None):
        self.error = error
        self.mutate = mutate
        self.kwargs = None

    async def run_async(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        if self.mutate:
            self.mutate()
        for i in range(2):
            yield {"event": i}


class _LlmCallsLimitExceededError(Exception):
    pass


# ─── reading session state ─────────────────────────────────────────────


def test_the_final_state_is_read_back():
    svc = _SessionSvc({"verdict": "pass"})
    assert asyncio.run(pl._session_state(svc, "sess-1")) == {"verdict": "pass"}


def test_an_unreadable_session_reads_as_empty():
    class _Broken:
        async def get_session(self, **kw):
            raise RuntimeError("gone")
    assert asyncio.run(pl._session_state(_Broken(), "sess-1")) == {}


# ─── driving a single agent ────────────────────────────────────────────


@pytest.fixture()
def no_deadline(monkeypatch):
    monkeypatch.setattr(pl, "_pipeline_deadline_s", lambda: 0)


def test_a_single_agent_run_returns_its_state(no_deadline):
    svc = _SessionSvc({"live_verdict": "PASS"})
    out = asyncio.run(pl._drive_single(_Runner(), svc, "sess-1", {"a": 1}))
    assert out == {"live_verdict": "PASS"}


def test_a_single_agent_abort_recovers_partial_state(no_deadline):
    svc = _SessionSvc({"partial": True})
    out = asyncio.run(pl._drive_single(_Runner(error=RuntimeError("boom")),
                                       svc, "sess-1", {}))
    assert out["partial"] is True and out["_pipeline_abort"] == "RuntimeError"


def test_a_single_agent_deadline_is_tagged_as_such(monkeypatch):
    monkeypatch.setattr(pl, "_pipeline_deadline_s", lambda: 0)
    svc = _SessionSvc()
    out = asyncio.run(pl._drive_single(_Runner(error=TimeoutError()), svc,
                                       "sess-1", {}))
    assert out["_pipeline_abort"] == "deadline"


def test_a_configured_deadline_wraps_the_run(monkeypatch):
    monkeypatch.setattr(pl, "_pipeline_deadline_s", lambda: 5)
    svc = _SessionSvc({"ok": True})
    assert asyncio.run(pl._drive_single(_Runner(), svc, "sess-1", {}))["ok"] is True


# ─── driving the pipeline ──────────────────────────────────────────────


def test_a_pipeline_run_returns_its_state(no_deadline):
    svc = _SessionSvc({"feedback_verdict": "pass"})
    out = asyncio.run(pl._drive_pipeline(_Runner(), svc, "sess-1", "content"))
    assert out == {"feedback_verdict": "pass"}


def test_the_call_ceiling_rides_on_the_run(no_deadline, monkeypatch):
    monkeypatch.setenv("AIFORGE_MAX_LLM_CALLS", "9")
    runner = _Runner()
    asyncio.run(pl._drive_pipeline(runner, _SessionSvc(), "sess-1", "c"))
    assert runner.kwargs["run_config"].max_llm_calls == 9
    assert runner.kwargs["new_message"] == "c"


def test_a_run_config_that_cannot_be_built_is_skipped(no_deadline, monkeypatch):
    monkeypatch.setattr(pl, "_pipeline_run_config", lambda: None)
    runner = _Runner()
    asyncio.run(pl._drive_pipeline(runner, _SessionSvc(), "sess-1", "c"))
    assert "run_config" not in runner.kwargs


def test_hitting_the_call_cap_lands_the_ticket_blocked(no_deadline):
    """A stuck local Doer aborts with partial state instead of hanging."""
    svc = _SessionSvc({"plan_md": "the plan"})
    out = asyncio.run(pl._drive_pipeline(
        _Runner(error=_LlmCallsLimitExceededError("max_llm_calls reached")),
        svc, "sess-1", "c"))
    assert out["plan_md"] == "the plan"
    assert out["feedback_verdict"] == "fail"
    assert out["_pipeline_abort"] == "_LlmCallsLimitExceededError"


def test_a_pipeline_deadline_is_tagged_as_such(no_deadline):
    out = asyncio.run(pl._drive_pipeline(_Runner(error=TimeoutError()),
                                         _SessionSvc(), "sess-1", "c"))
    assert out["_pipeline_abort"] == "deadline"
    assert out["feedback_verdict"] == "fail"


def test_any_other_crash_is_also_soft(no_deadline):
    out = asyncio.run(pl._drive_pipeline(_Runner(error=ValueError("bad state")),
                                         _SessionSvc(), "sess-1", "c"))
    assert out["_pipeline_abort"] == "ValueError"


# ─── the single-agent wrapper ──────────────────────────────────────────


@pytest.fixture()
def single(monkeypatch):
    """Stub the ADK runner/session plumbing for _run_single_agent."""
    import google.adk.runners as runners
    import google.adk.sessions as sessions
    state: dict = {"svc": _SessionSvc({"live_verdict": "PASS"}),
                   "runner": _Runner(), "destroyed": []}
    monkeypatch.setattr(sessions, "InMemorySessionService", lambda: state["svc"])
    monkeypatch.setattr(runners, "Runner", lambda **kw: state["runner"])
    monkeypatch.setattr(pl, "_key_stateful_tools",
                        lambda sid: state.setdefault("keyed", sid))
    from aiforge_core.runtime.tools import bash
    monkeypatch.setattr(bash, "destroy_session",
                        lambda sid: state["destroyed"].append(sid))
    monkeypatch.setattr(pl, "_pipeline_deadline_s", lambda: 0)
    return state


def _ticket(**kw):
    base = {"id": 1, "identifier": "ONE-1", "title": "Fix it", "body": "details",
            "project": "app", "metadata": {}}
    base.update(kw)
    return pytypes.SimpleNamespace(**base)


def test_a_single_agent_run_seeds_the_ticket_and_cleans_up(single):
    out = asyncio.run(pl._run_single_agent(object(), "the prompt",
                                           ticket=_ticket()))
    assert out == {"live_verdict": "PASS"}
    assert single["svc"].created["state"] == {"ticket_identifier": "ONE-1",
                                              "ticket_project": "app"}
    assert single["keyed"] == "sess-1" and single["destroyed"] == ["sess-1"]


def test_a_ticketless_run_seeds_nothing(single):
    asyncio.run(pl._run_single_agent(object(), "prompt"))
    assert single["svc"].created["state"] is None


def test_the_session_is_destroyed_even_when_the_run_fails(single):
    single["runner"] = _Runner(error=RuntimeError("boom"))
    asyncio.run(pl._run_single_agent(object(), "prompt"))
    assert single["destroyed"] == ["sess-1"]


def test_a_failing_teardown_does_not_mask_the_result(single, monkeypatch):
    from aiforge_core.runtime.tools import bash
    monkeypatch.setattr(bash, "destroy_session",
                        lambda sid: (_ for _ in ()).throw(RuntimeError("no tmux")))
    assert asyncio.run(pl._run_single_agent(object(), "p"))["live_verdict"] == "PASS"


# ─── the post-PR live verifier ─────────────────────────────────────────


@pytest.fixture()
def verifier(monkeypatch, tmp_path):
    import aiforge_core.runtime.pipeline as pipe
    state: dict = {"prompt": None, "state": {"live_verifier_verdict": "PASS"},
                   "diff": "app/store.py | 12 +++"}
    monkeypatch.setattr(pipe, "build_live_verifier_agent",
                        lambda project=None: object())

    async def _run_single(agent, prompt, ticket=None):
        state["prompt"] = prompt
        state["pr_url_during"] = os.environ.get("PR_URL")
        return state["state"]
    monkeypatch.setattr(pl, "_run_single_agent", _run_single)
    monkeypatch.setattr(pl, "_extract_live_verifier",
                        lambda st: {"verdict": st.get("live_verifier_verdict")})
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: pytypes.SimpleNamespace(stdout=state["diff"]))
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("PR_URL", raising=False)
    return state


def test_the_verifier_is_seeded_with_the_pr_and_the_diff(verifier):
    """It needs to know what changed without inheriting the whole pipeline
    history, which overflowed the model."""
    out = pl._run_live_verifier(_ticket(), "https://github.com/o/r/pull/1")
    assert out == {"verdict": "PASS"}
    prompt = verifier["prompt"]
    assert "https://github.com/o/r/pull/1" in prompt
    assert "app/store.py | 12 +++" in prompt
    assert "# Ticket ONE-1" in prompt


def test_pr_url_is_exported_for_the_recipe_then_restored(verifier):
    """The deploy recipe's bash uses $PR_URL and the auto-merge gate reads it."""
    pl._run_live_verifier(_ticket(), "https://github.com/o/r/pull/1")
    assert verifier["pr_url_during"] == "https://github.com/o/r/pull/1"
    assert "PR_URL" not in os.environ


def test_a_pre_existing_pr_url_is_put_back(verifier, monkeypatch):
    monkeypatch.setenv("PR_URL", "https://github.com/o/r/pull/0")
    pl._run_live_verifier(_ticket(), "https://github.com/o/r/pull/1")
    assert os.environ["PR_URL"] == "https://github.com/o/r/pull/0"


def test_an_unavailable_diff_stat_does_not_stop_the_verifier(verifier, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert pl._run_live_verifier(_ticket(), "u") == {"verdict": "PASS"}


def test_a_ticket_with_no_body_still_gets_a_prompt(verifier):
    pl._run_live_verifier(_ticket(body=""), "u")
    assert "(no body)" in verifier["prompt"]
