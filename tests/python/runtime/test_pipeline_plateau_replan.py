"""A stalled Doer loop is re-planned with a different approach (bounded) before
partial work ships for review: finishing the task comes first."""
from types import SimpleNamespace

from aiforge_core.runtime.graph_pipeline import _gates as G


def _ctx(**state):
    return SimpleNamespace(state=dict(state), route=None)


def _stalled(**extra):
    return dict(feedback_verdict="partial loop_budget_kill: same_failure",
                loop_budget_reason="same_failure", doer_iters=4, **extra)


def test_a_stalled_loop_gets_a_different_plan_first(monkeypatch):
    monkeypatch.setattr(G, "PLATEAU_REPLANS", 2)
    ctx = _ctx(**_stalled(loop_budget_kill=True))
    G._validator_gate(ctx)
    assert ctx.route == G.ROUTE_REPLAN
    assert ctx.state["plateau_replan_count"] == 1
    assert "DIFFERENT approach" in ctx.state["replan_note"]
    assert ctx.state["doer_iters"] == 0                    # a clean loop
    assert "loop_budget_kill" not in ctx.state


def test_after_the_bound_the_partial_work_ships(monkeypatch):
    monkeypatch.setattr(G, "PLATEAU_REPLANS", 2)
    ctx = _ctx(**_stalled(plateau_replan_count=2))
    G._validator_gate(ctx)
    assert ctx.route == G.ROUTE_DONE
    assert ctx.state["_no_replan_reason"] == "doer_plateau"


def test_zero_keeps_the_old_ship_at_the_first_stall(monkeypatch):
    monkeypatch.setattr(G, "PLATEAU_REPLANS", 0)
    ctx = _ctx(**_stalled())
    G._validator_gate(ctx)
    assert ctx.route == G.ROUTE_DONE


def test_test_gaming_is_still_never_replanned(monkeypatch):
    monkeypatch.setattr(G, "PLATEAU_REPLANS", 2)
    ctx = _ctx(**_stalled(quality_issue="test_gaming"))
    G._validator_gate(ctx)
    assert ctx.route == G.ROUTE_DONE
