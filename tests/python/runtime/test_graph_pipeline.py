"""Tests for the Workflow routing nodes (runtime.graph_pipeline) and the
end-to-end graph execution (trivial / full / loop / replan)."""
from __future__ import annotations

import asyncio

import pytest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START, Edge, Workflow, node
from google.genai import types as gtypes

from aiforge_core.runtime import graph_pipeline as gp


class _FakeCtx:
    """Minimal Context stand-in for unit-testing a router node body."""

    def __init__(self, state: dict) -> None:
        self.state = state
        self.route = None


def _run(coro):
    return asyncio.run(coro)


# ── pure helpers ───────────────────────────────────────────────────────

def test_read_complexity_explicit_key() -> None:
    assert gp._read_complexity({"complexity": "Trivial"}) == "trivial"


def test_read_complexity_triage_dict() -> None:
    assert gp._read_complexity({"triage_verdict": {"complexity": "hard"}}) == "hard"


def test_read_complexity_triage_json_string() -> None:
    assert gp._read_complexity({"triage_verdict": '{"complexity": "trivial"}'}) == "trivial"


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
])
def test_validator_failed(raw, expected) -> None:
    assert gp._validator_failed({"validator_verdict": raw}) is expected


@pytest.mark.parametrize("raw,expected", [
    ('{"verdict": "pass"}', True),
    ({"verdict": "approve"}, True),
    ('{"verdict": "pass_with_warnings"}', True),
    ('{"verdict": "fail"}', False),
    (None, False),
])
def test_feedback_passed(raw, expected) -> None:
    assert gp._feedback_passed({"feedback_verdict": raw}) is expected


# ── router node bodies ─────────────────────────────────────────────────

def test_triage_gate_trivial_routes_trivial() -> None:
    ctx = _FakeCtx({"complexity": "trivial"})
    _run(gp._triage_gate(ctx))
    assert ctx.route == gp.ROUTE_TRIVIAL


def test_triage_gate_default_routes_full() -> None:
    ctx = _FakeCtx({})
    _run(gp._triage_gate(ctx))
    assert ctx.route == gp.ROUTE_FULL


def test_loop_gate_exits_on_pass() -> None:
    ctx = _FakeCtx({"feedback_verdict": '{"verdict": "pass"}'})
    _run(gp._loop_gate(ctx))
    assert ctx.route == gp.ROUTE_EXIT


def test_loop_gate_loops_then_exits_at_cap() -> None:
    state = {"feedback_verdict": '{"verdict": "fail"}'}
    ctx = _FakeCtx(state)
    _run(gp._loop_gate(ctx))
    assert ctx.route == gp.ROUTE_LOOP and state["doer_iters"] == 1
    _run(gp._loop_gate(ctx))
    assert ctx.route == gp.ROUTE_LOOP and state["doer_iters"] == 2
    _run(gp._loop_gate(ctx))  # hits MAX_DOER_ITERS=3
    assert ctx.route == gp.ROUTE_EXIT and state["doer_iters"] == 3


def test_loop_gate_kill_flag_exits() -> None:
    ctx = _FakeCtx({"feedback_verdict": '{"verdict": "fail"}', "loop_budget_kill": True})
    _run(gp._loop_gate(ctx))
    assert ctx.route == gp.ROUTE_EXIT


def test_validator_gate_replan_once_then_done() -> None:
    state = {"validator_verdict": {"verdict": "request_changes"}}
    ctx = _FakeCtx(state)
    _run(gp._validator_gate(ctx))
    assert ctx.route == gp.ROUTE_REPLAN
    assert state["replan_count"] == 1 and state["doer_iters"] == 0
    assert "replan_note" in state
    # second time: replan budget spent → done
    ctx2 = _FakeCtx(state)
    _run(gp._validator_gate(ctx2))
    assert ctx2.route == gp.ROUTE_DONE


def test_validator_gate_pass_routes_done() -> None:
    ctx = _FakeCtx({"validator_verdict": {"verdict": "approve"}})
    _run(gp._validator_gate(ctx))
    assert ctx.route == gp.ROUTE_DONE


# ── full graph execution with stub agent nodes ─────────────────────────

def _build_stub_graph(order, *, validator_sets):
    """Mirror the real pipeline topology but stub the agents as recording
    FunctionNodes, wired through the REAL gate + merge nodes."""

    def agent_stub(name, sets=None):
        async def _fn(ctx):
            order.append(name)
            for k, v in (sets or {}).items():
                ctx.state[k] = v
        return node(_fn, name=name)

    enhancer = agent_stub("enhancer")
    researcher = agent_stub("researcher", {"research_brief_md": "r"})
    ctx_mem = agent_stub("ctx_memory", {"memory_brief_md": "m"})
    planner = agent_stub("planner")
    vcorr = agent_stub("verify_correctness", {"verify_correctness": '{"verdict":"pass"}'})
    vscope = agent_stub("verify_scope", {"verify_scope": '{"verdict":"pass"}'})
    doer = agent_stub("doer")
    refiner = agent_stub("refiner")
    feedback = agent_stub("feedback", {"feedback_verdict": '{"verdict":"pass"}'})
    learner = agent_stub("learner")
    validator = agent_stub("validator", validator_sets)

    from aiforge_core.runtime import parallel_stages as ps
    cjoin, mctx = ps.make_context_join(), ps.make_merge_context_node()
    vjoin, mver = ps.make_verifier_join(), ps.make_merge_verdicts_node()
    triage, loopg, valg = (
        gp.make_triage_gate(), gp.make_loop_gate(), gp.make_validator_gate())

    ctx_branches = [researcher, ctx_mem]
    ver_branches = [vcorr, vscope]
    edges = [
        Edge(from_node=START, to_node=triage),
        Edge(from_node=triage, to_node=doer, route=gp.ROUTE_TRIVIAL),
        Edge(from_node=triage, to_node=enhancer, route=gp.ROUTE_FULL),
    ]
    for br in ctx_branches:
        edges += [Edge(from_node=enhancer, to_node=br),
                  Edge(from_node=br, to_node=cjoin)]
    edges += [Edge(from_node=cjoin, to_node=mctx),
              Edge(from_node=mctx, to_node=planner)]
    for br in ver_branches:
        edges += [Edge(from_node=planner, to_node=br),
                  Edge(from_node=br, to_node=vjoin)]
    edges += [
        Edge(from_node=vjoin, to_node=mver),
        Edge(from_node=mver, to_node=doer),
        Edge(from_node=doer, to_node=refiner),
        Edge(from_node=refiner, to_node=feedback),
        Edge(from_node=feedback, to_node=loopg),
        Edge(from_node=loopg, to_node=doer, route=gp.ROUTE_LOOP),
        Edge(from_node=loopg, to_node=validator, route=gp.ROUTE_EXIT),
        Edge(from_node=validator, to_node=valg),
        Edge(from_node=valg, to_node=planner, route=gp.ROUTE_REPLAN),
        Edge(from_node=valg, to_node=learner, route=gp.ROUTE_DONE),
    ]
    return Workflow(name="stub", edges=edges)


def _drive(wf, initial_state):
    async def _go():
        svc = InMemorySessionService()
        r = Runner(agent=wf, app_name="t", session_service=svc,
                   auto_create_session=True)
        s = await svc.create_session(app_name="t", user_id="u",
                                     state=initial_state or None)
        c = gtypes.Content(role="user", parts=[gtypes.Part.from_text(text="go")])
        async for _ in r.run_async(user_id="u", session_id=s.id, new_message=c):
            pass
        s2 = await svc.get_session(app_name="t", user_id="u", session_id=s.id)
        return dict(s2.state or {})
    return asyncio.run(_go())


def test_graph_full_path_order_and_merges() -> None:
    order: list = []
    wf = _build_stub_graph(order, validator_sets={
        "validator_verdict": '{"verdict":"approve"}'})
    state = _drive(wf, {"complexity": "moderate"})
    # enhancer → parallel ctx → planner → parallel verify → doer loop →
    # validator → learner
    assert order[0] == "enhancer"
    assert order.index("planner") < order.index("doer")
    assert order[-1] == "learner"
    assert {"researcher", "ctx_memory"} <= set(order)
    assert {"verify_correctness", "verify_scope"} <= set(order)
    # merges ran
    assert "context_brief_md" in state
    assert state.get("verifier_verdict", {}).get("verdict") == "pass"


def test_graph_trivial_path_skips_planning() -> None:
    order: list = []
    wf = _build_stub_graph(order, validator_sets={
        "validator_verdict": '{"verdict":"approve"}'})
    _drive(wf, {"complexity": "trivial"})
    assert "enhancer" not in order
    assert "planner" not in order
    assert order[0] == "doer"
    assert order[-1] == "learner"


def test_graph_replan_loops_back_to_planner_once() -> None:
    order: list = []
    wf = _build_stub_graph(order, validator_sets={
        "validator_verdict": '{"verdict":"request_changes"}'})
    state = _drive(wf, {"complexity": "moderate"})
    # validator requests changes → replan → planner runs twice
    assert order.count("planner") == 2
    assert order.count("validator") == 2
    assert order[-1] == "learner"
    assert state.get("replan_count") == 1
