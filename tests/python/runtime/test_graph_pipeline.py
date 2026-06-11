"""Tests for GraphPipeline — the custom BaseAgent graph router
(runtime.graph_pipeline)."""
from __future__ import annotations

import asyncio

import pytest
from google.adk.agents import BaseAgent

from aiforge_core.runtime import graph_pipeline as gp
from aiforge_core.runtime.graph_pipeline import GraphPipeline

# ── pure helpers ───────────────────────────────────────────────────────

def test_read_complexity_explicit_key() -> None:
    assert gp._read_complexity({"complexity": "Trivial"}) == "trivial"


def test_read_complexity_triage_dict() -> None:
    assert gp._read_complexity({"triage_verdict": {"complexity": "hard"}}) == "hard"


def test_read_complexity_triage_json_string() -> None:
    state = {"triage_verdict": '{"complexity": "trivial"}'}
    assert gp._read_complexity(state) == "trivial"


def test_read_complexity_default_moderate() -> None:
    assert gp._read_complexity({}) == "moderate"
    assert gp._read_complexity({"triage_verdict": "junk"}) == "moderate"


@pytest.mark.parametrize("raw,expected", [
    ({"verdict": "request_changes"}, True),
    ({"verdict": "approve"}, False),
    ('{"verdict": "reject"}', True),
    ('{"verdict": "approve"}', False),
    ("request_changes", True),
    (None, False),
    ({}, False),
])
def test_validator_failed(raw, expected) -> None:
    assert gp._validator_failed({"validator_verdict": raw}) is expected


# ── routing integration via a real Runner + stub agents ────────────────


class _Recorder:
    """Shared call log — an arbitrary object so pydantic keeps it by
    reference instead of copying it like a plain list field."""

    def __init__(self) -> None:
        self.calls: list = []


class _Stub(BaseAgent):
    """Records its name when run; optionally mutates session state."""

    rec: object
    sets: dict = {}

    async def _run_async_impl(self, ctx):  # type: ignore[no-untyped-def]
        self.rec.calls.append(self.name)
        for k, v in (self.sets or {}).items():
            ctx.session.state[k] = v
        if False:  # make this an async generator that yields nothing
            yield


def _make_graph(rec, *, validator_sets=None):
    def stub(name, sets=None):
        return _Stub(name=name, rec=rec, sets=sets or {})

    enhancer = stub("enhancer")
    context_gather = stub("context_gather")
    planner = stub("planner")
    verifier = stub("verifier")
    doer_loop = stub("doer_refiner_feedback_loop")
    learner = stub("learner")
    validator = stub("validator", sets=validator_sets)
    graph = GraphPipeline(
        name="g",
        sub_agents=[enhancer, context_gather, planner, verifier,
                    doer_loop, learner, validator],
        enhancer=enhancer, context_gather=context_gather, planner=planner,
        verifier=verifier, doer_loop=doer_loop, learner=learner,
        validator=validator,
    )
    return graph


def _drive(graph, initial_state):
    async def _go():
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types as gtypes

        svc = InMemorySessionService()
        runner = Runner(agent=graph, app_name="t", session_service=svc,
                        auto_create_session=True)
        session = await svc.create_session(
            app_name="t", user_id="u", state=initial_state or None)
        content = gtypes.Content(
            role="user", parts=[gtypes.Part.from_text(text="go")])
        async for _ in runner.run_async(
                user_id="u", session_id=session.id, new_message=content):
            pass
        s = await svc.get_session(app_name="t", user_id="u",
                                  session_id=session.id)
        return dict(s.state or {})

    return asyncio.run(_go())


def test_full_path_runs_all_stages_in_order() -> None:
    rec = _Recorder()
    graph = _make_graph(rec, validator_sets={
        "validator_verdict": {"verdict": "approve"}})
    state = _drive(graph, {"complexity": "moderate"})
    assert rec.calls == [
        "enhancer", "context_gather", "planner", "verifier",
        "doer_refiner_feedback_loop", "validator", "learner",
    ]
    assert state.get("graph_route", {}).get("complexity") == "moderate"


def test_trivial_path_skips_planning() -> None:
    rec = _Recorder()
    graph = _make_graph(rec, validator_sets={
        "validator_verdict": {"verdict": "approve"}})
    _drive(graph, {"complexity": "trivial"})
    # fast path: only doer loop + validator, no enhancer/planner/verifier
    assert rec.calls == ["doer_refiner_feedback_loop", "validator"]


def test_replan_edge_loops_back_once_on_failure() -> None:
    rec = _Recorder()
    # validator always requests changes → replan should fire exactly once
    graph = _make_graph(rec, validator_sets={
        "validator_verdict": {"verdict": "request_changes"}})
    state = _drive(graph, {"complexity": "hard"})
    # planner/verifier/doer/validator run TWICE (initial + 1 replan),
    # enhancer/context once, learner once at the very end.
    assert rec.calls.count("planner") == 2
    assert rec.calls.count("validator") == 2
    assert rec.calls.count("enhancer") == 1
    assert rec.calls.count("learner") == 1
    assert rec.calls[-1] == "learner"
    assert state.get("replan_count") == 1
    assert "replan_note" in state
