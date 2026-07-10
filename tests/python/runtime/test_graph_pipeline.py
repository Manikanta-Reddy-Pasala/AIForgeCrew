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
    # 'low' is a base-tier complexity (not moderate/complex) so the effective
    # iteration cap is the flat MAX_DOER_ITERS floor — an unset complexity now
    # defaults to 'moderate' (→ 20 iters) under the dynamic tiered budget.
    state = {"feedback_verdict": '{"verdict": "fail"}', "complexity": "low"}
    ctx = _FakeCtx(state)
    # Loop while under the cap; exit exactly at MAX_DOER_ITERS (default 4).
    for i in range(1, gp.MAX_DOER_ITERS):
        _run(gp._loop_gate(ctx))
        assert ctx.route == gp.ROUTE_LOOP and state["doer_iters"] == i
    _run(gp._loop_gate(ctx))  # hits MAX_DOER_ITERS
    assert ctx.route == gp.ROUTE_EXIT and state["doer_iters"] == gp.MAX_DOER_ITERS


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


def test_validator_gate_plateau_no_replan() -> None:
    # Doer loop exited on a loop_budget_kill (LOC-plateau / wall-clock). The
    # Validator wants changes, but a replan just re-runs the same local model
    # on already-attempted work and it re-plateaus — a wasted cycle. The gate
    # must NOT replan; it routes straight to done (runner ships partial+PR →
    # in_review). Guards against the ONE-157 24-min churn on a committed diff.
    state = {
        "validator_verdict": {"verdict": "request_changes"},
        "feedback_verdict": "partial loop_budget_kill: loc_plateau:4x<3_after_900s",
    }
    ctx = _FakeCtx(state)
    _run(gp._validator_gate(ctx))
    assert ctx.route == gp.ROUTE_DONE
    assert int(state.get("replan_count", 0)) == 0  # did NOT consume a replan
    assert state.get("_no_replan_reason") == "doer_plateau"


def test_validator_gate_replan_clears_stale_loop_state() -> None:
    # prior pass left a 'pass' feedback verdict + kill flag; replan must
    # wipe them so the next Doer loop doesn't exit at zero iterations.
    state = {
        "validator_verdict": {"verdict": "request_changes"},
        "feedback_verdict": '{"verdict": "pass"}',
        "loop_budget_kill": True,
        "doer_outcome": "old",
        "verifier_verdict": {"verdict": "pass"},
        "verify_correctness": '{"verdict": "pass"}',
    }
    _run(gp._validator_gate(_FakeCtx(state)))
    for k in ("feedback_verdict", "loop_budget_kill", "doer_outcome",
              "verifier_verdict", "verify_correctness"):
        assert k not in state, k
    assert state["doer_iters"] == 0


def test_verifier_gate_reject_routes_replan_then_pass() -> None:
    state = {"verifier_verdict": {"verdict": "reject", "rationale": "no test"}}
    ctx = _FakeCtx(state)
    _run(gp._verifier_gate(ctx))
    assert ctx.route == gp.ROUTE_VERIFY_REPLAN
    assert state["verify_replan_count"] == 1
    assert "replan_note" in state
    # per-axis verdicts cleared for a fresh re-verify
    assert "verifier_verdict" not in state
    # budget spent → next reject proceeds to the Doer anyway
    state["verifier_verdict"] = {"verdict": "reject"}
    ctx2 = _FakeCtx(state)
    _run(gp._verifier_gate(ctx2))
    assert ctx2.route == gp.ROUTE_VERIFY_PASS


def test_verifier_gate_pass_routes_to_doer() -> None:
    ctx = _FakeCtx({"verifier_verdict": {"verdict": "pass"}})
    _run(gp._verifier_gate(ctx))
    assert ctx.route == gp.ROUTE_VERIFY_PASS


# ── full graph execution with stub agent nodes ─────────────────────────

def _build_stub_graph(order, *, validator_sets, verifier_rejects=False):
    """Mirror the real pipeline topology but stub the agents as recording
    FunctionNodes, wired through the REAL gate + merge nodes."""

    def agent_stub(name, sets=None):
        async def _fn(ctx):
            order.append(name)
            for k, v in (sets or {}).items():
                ctx.state[k] = v
        return node(_fn, name=name)

    _v = ('{"verdict":"reject", "rationale": "bad plan"}'
          if verifier_rejects else '{"verdict":"pass"}')
    enhancer = agent_stub("enhancer")
    researcher = agent_stub("researcher", {"research_brief_md": "r"})
    ctx_mem = agent_stub("ctx_memory", {"memory_brief_md": "m"})
    planner = agent_stub("planner")
    vcorr = agent_stub("verify_correctness", {"verify_correctness": _v})
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
    verg = gp.make_verifier_gate()

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
        Edge(from_node=mver, to_node=verg),
        Edge(from_node=verg, to_node=doer, route=gp.ROUTE_VERIFY_PASS),
        Edge(from_node=verg, to_node=planner, route=gp.ROUTE_VERIFY_REPLAN),
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


def test_graph_verifier_validator_pingpong_is_bounded() -> None:
    """Worst case: verifier ALWAYS rejects + validator ALWAYS rejects.
    validator-replan clears verify_replan_count, so the budgets compose:
    (MAX_REPLANS+1) × (MAX_VERIFY_REPLANS+1) = 4 planner runs max — the
    graph must still terminate at learner, never spin."""
    order: list = []
    wf = _build_stub_graph(
        order,
        validator_sets={"validator_verdict": '{"verdict":"request_changes"}'},
        verifier_rejects=True,
    )
    state = _drive(wf, {"complexity": "moderate"})
    expected = (gp.MAX_REPLANS + 1) * (gp.MAX_VERIFY_REPLANS + 1)
    assert order.count("planner") == expected, order
    assert order[-1] == "learner"
    assert state.get("replan_count") == gp.MAX_REPLANS


# ── research-gap loop ──────────────────────────────────────────────────

def test_research_gap_loop_redispatches_once_then_proceeds() -> None:
    """gap_eval judges insufficient on pass 1 → gap_gate routes back to
    research_entry (researcher re-runs) → pass 2 judged sufficient →
    proceeds to planner. Researcher runs exactly twice; bounded."""
    order: list = []

    def agent_stub(name, fn=None):
        async def _fn(ctx):
            order.append(name)
            if fn:
                fn(ctx.state)
        return node(_fn, name=name)

    def _gap_eval_body(state):
        # insufficient until one gap pass has been spent
        if int(state.get("gap_pass_count", 0) or 0) == 0:
            state["gap_verdict"] = '{"sufficient": false, "missing": ["x"]}'
        else:
            state["gap_verdict"] = '{"sufficient": true}'

    enhancer = agent_stub("enhancer")
    researcher = agent_stub("researcher",
                            lambda s: s.__setitem__("research_brief_md", "r"))
    gap_eval = agent_stub("gap_eval", _gap_eval_body)
    planner = agent_stub("planner")

    from aiforge_core.runtime import parallel_stages as ps
    rentry = ps.make_research_entry_node()
    cjoin, mctx = ps.make_context_join(), ps.make_merge_context_node()
    gapg = gp.make_gap_gate()

    edges = [
        Edge(from_node=START, to_node=enhancer),
        Edge(from_node=enhancer, to_node=rentry),
        Edge(from_node=rentry, to_node=researcher),
        Edge(from_node=researcher, to_node=cjoin),
        Edge(from_node=cjoin, to_node=mctx),
        Edge(from_node=mctx, to_node=gap_eval),
        Edge(from_node=gap_eval, to_node=gapg),
        Edge(from_node=gapg, to_node=planner, route=gp.ROUTE_RESEARCH_OK),
        Edge(from_node=gapg, to_node=rentry, route=gp.ROUTE_RESEARCH_GAP),
    ]
    wf = Workflow(name="gap_stub", edges=edges)
    state = _drive(wf, {})
    assert order.count("researcher") == 2, order
    assert order.count("planner") == 1, order
    assert order[-1] == "planner"
    assert state.get("gap_pass_count") == 1
