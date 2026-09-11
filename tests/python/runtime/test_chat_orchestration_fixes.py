"""Chat-side orchestration fixes from the 2026-09-11 review.

- Team runs never keyed their stateful tools, so every team run shared the
  "default" tmux shell: a run on repo B executed its commands in the directory
  repo A's run had cd'd into, and nothing was torn down afterwards.
- Team runs had no wall-clock deadline (only an LLM-call cap) while holding a
  server-wide lock, and replayed every event on every call (no context filter).
- delegate_to_agent could never work: it looked a role up in the pipeline's
  ``sub_agents``, and the pipeline is a Workflow graph with none.
"""
from __future__ import annotations

import asyncio
import pathlib
import queue

import pytest

from aiforge_core.runtime import run_resources
from aiforge_core.runtime.tools import bash

REPO = pathlib.Path(__file__).resolve().parents[3]


# ── one shell per run ──────────────────────────────────────────────────────

def test_each_run_gets_its_own_shell_session():
    import contextvars
    seen = []

    def run(run_id):
        run_resources.key_stateful_tools(run_id)
        seen.append(bash._effective_run_id(None))
    contextvars.copy_context().run(run, "run-A")
    contextvars.copy_context().run(run, "run-B")
    assert seen == ["run-A", "run-B"]            # not both "default"


def test_teardown_reaches_every_keyed_resource(monkeypatch):
    killed = []
    from aiforge_core.runtime.tools import browser, ipython_kernel
    monkeypatch.setattr(bash, "destroy_session", lambda rid: killed.append(("bash", rid)))
    monkeypatch.setattr(browser, "destroy_context", lambda rid: killed.append(("browser", rid)))
    monkeypatch.setattr(ipython_kernel, "destroy_kernel", lambda rid: killed.append(("ipython", rid)))
    run_resources.destroy_run_resources("run-A")
    assert ("bash", "run-A") in killed
    assert ("browser", "run-A") in killed
    assert ("ipython", "run-A") in killed


def test_team_chat_keys_and_tears_down_its_run():
    src = (REPO / "aiforge_core" / "runtime" / "chat_pipeline.py").read_text()
    assert "key_stateful_tools(_run_id)" in src
    assert "destroy_run_resources(_run_id)" in src


# ── a deadline, and the ticket path's context filter ──────────────────────

def test_a_stalled_team_run_is_stopped_at_its_deadline(monkeypatch):
    from aiforge_core.runtime import chat_pipeline as cp
    monkeypatch.setenv("AIFORGE_CHAT_TEAM_DEADLINE_S", "0.2")
    closed = {}

    async def _stuck(*_a, **_k):
        await asyncio.sleep(30)

    class _Agen:
        async def aclose(self):
            closed["yes"] = True

    monkeypatch.setattr(cp, "_drive_run_events", _stuck)
    q: queue.Queue = queue.Queue()
    out = asyncio.run(cp._events_under_deadline(_Agen(), None, q, 1, None, []))
    assert out is None
    assert closed.get("yes")
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert events[0]["type"] == "error"
    assert "deadline" in events[0]["text"]
    assert events[1] == {"type": "stopped", "reason": "deadline"}


def test_the_team_deadline_defaults_to_the_pipelines(monkeypatch):
    from aiforge_core.runtime import chat_pipeline as cp
    monkeypatch.delenv("AIFORGE_CHAT_TEAM_DEADLINE_S", raising=False)
    monkeypatch.delenv("AIFORGE_PIPELINE_DEADLINE_S", raising=False)
    assert cp._team_deadline_s() == 5400.0


def test_team_chat_uses_the_context_filter():
    pytest.importorskip("google.adk.plugins.context_filter_plugin")
    from aiforge_core.runtime import chat_pipeline as cp
    names = [type(p).__name__ for p in cp._team_plugins()]
    assert "ContextFilterPlugin" in names
    assert "PhantomToolGuardPlugin" in names


# ── delegate_to_agent builds the role it is asked for ──────────────────────

@pytest.mark.parametrize("role", ["researcher", "planner", "refiner", "triage", "verifier"])
def test_every_delegable_role_builds(role):
    pytest.importorskip("google.adk")
    from aiforge_core.runtime.tools import delegation
    agent = delegation._build_delegate_agent(role)
    assert agent is not None, f"{role}: delegate_build_failed"


def test_unknown_roles_build_nothing():
    from aiforge_core.runtime.pipeline import build_role_agent
    assert build_role_agent("doer") is None
    assert build_role_agent("") is None
