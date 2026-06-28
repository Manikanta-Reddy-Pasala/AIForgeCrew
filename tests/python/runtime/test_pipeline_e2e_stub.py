"""End-to-end regression: the REAL build_pipeline graph with stub LLMs.

The topology tests in test_graph_pipeline.py stub agents as FunctionNodes,
which structurally cannot catch LlmAgent-node engine semantics — the P0
this file guards: ADK's graph builder clones every LlmAgent and forces
``wait_for_output=True`` for chat mode; chat nodes never yield engine
"output", so the whole graph silently stalled after the enhancer. This
drives the actual Workflow with stub BaseLlm models and asserts every
stage executes.
"""
from __future__ import annotations

import asyncio

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types as gt


def _make_stub(role: str, calls: list):
    replies = {
        "triage": '{"complexity": "moderate", "estimated_files": 3, '
                  '"rationale": "x"}',
        "feedback": "pass\nall good",
        "validator": '{"verdict": "approve", "rationale": "ok", '
                     '"scope_ok": true, "tests_present": true, '
                     '"regression_risk": "low"}',
        "planner": 'PLAN: {"subtickets": '
                   '[{"scope_allowlist_globs": ["src/a/**"]}]}',
        "verifier": '{"verdict": "pass", "rationale": "ok"}',
    }

    class _Stub(BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            calls.append(role)
            text = replies.get(role, f"{role} output")
            yield LlmResponse(content=gt.Content(
                role="model", parts=[gt.Part(text=text)]))

    return _Stub(model="stub")


@pytest.fixture
def _stub_pipeline(monkeypatch):
    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")
    monkeypatch.setenv("AIFORGE_OBSERVABILITY_DISABLE", "1")
    import aiforge_core.runtime.pipeline as pl
    calls: list = []
    monkeypatch.setattr(pl, "build_litellm_model",
                        lambda role: _make_stub(role, calls))
    return pl, calls


def _drive(workflow, prompt: str) -> dict:
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    async def _go():
        svc = InMemorySessionService()
        runner = Runner(agent=workflow, app_name="t", session_service=svc,
                        auto_create_session=True)
        session = await svc.create_session(app_name="t", user_id="u")
        msg = gt.Content(role="user", parts=[gt.Part(text=prompt)])
        async for _ in runner.run_async(user_id="u", session_id=session.id,
                                        new_message=msg):
            pass
        s = await svc.get_session(app_name="t", user_id="u",
                                  session_id=session.id)
        return dict(s.state or {})

    return asyncio.run(asyncio.wait_for(_go(), timeout=120))


def test_full_graph_executes_every_stage(_stub_pipeline) -> None:
    """P0 guard: the run must NOT stall after the enhancer."""
    pl, calls = _stub_pipeline
    wf = pl.build_pipeline(skip_researcher=True)
    state = _drive(wf, "# Ticket T-1\nfix the thing")
    # every stage fired, in causal order
    assert calls[0] == "triage"
    for stage in ("enhancer", "ctx_repomap",
                  "ctx_conventions", "planner", "verifier",
                  "doer", "refiner",
                  "feedback", "validator", "learner"):
        assert stage in calls, f"{stage} never ran — graph stalled. {calls}"
    assert calls.index("planner") < calls.index("doer")
    assert calls.index("doer") < calls.index("validator")
    # state flowed: plan_promote pulled globs out of the plan,
    # the single verifier wrote the verdict shape
    assert state.get("scope_allowlist_globs") == ["src/a/**"]
    assert state.get("verifier_verdict", {}).get("verdict") == "pass"
    assert str(state.get("feedback_verdict", "")).startswith("pass")


def test_graph_route_recorded(_stub_pipeline) -> None:
    """triage's verdict drives triage_gate and the chosen route is
    persisted to state. (The trivial route's topology is covered by the
    FunctionNode tests; the stub triage answers 'moderate'.)"""
    pl, calls = _stub_pipeline
    wf = pl.build_pipeline(skip_researcher=True)
    state = _drive(wf, "tiny fix")
    assert "doer" in calls and "learner" in calls
    assert state.get("graph_route", {}).get("complexity") == "moderate"