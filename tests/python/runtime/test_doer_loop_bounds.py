"""What actually STOPS the Doer loop.

The mechanisms were tested; every threshold that decides when the loop stops
was not — the module defaults, the complexity tiers, the plan-scaled term and
the wall-clock kill all survived mutation. These pin the numbers production
uses, and the state that must not survive an iteration.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import loop_budget
from aiforge_core.runtime.graph_pipeline import _config, _gates, _parsers


class _Ctx:
    def __init__(self, state):
        self.state = state
        self.route = None


async def _gate(state):
    ctx = _Ctx(state)
    await _gates._loop_gate(ctx)
    return ctx.route


def _run(state):
    import asyncio
    return asyncio.run(_gate(state))


# ── the defaults production actually uses ────────────────────────────────


def test_the_module_defaults_are_the_ones_documented():
    """`build_loop_budget_callbacks` uses these; the existing tests all pass
    thresholds explicitly, so a change to any default was invisible."""
    assert loop_budget._DEFAULT_PLATEAU_TURNS == 3
    assert loop_budget._DEFAULT_PLATEAU_DELTA == 50
    assert loop_budget._DEFAULT_MIN_ELAPSED_S == 600.0


def test_the_shipped_wall_budget_is_off():
    """0 = off. A non-zero default would kill every long loop at that many
    seconds, and no test referenced this knob at all."""
    assert _config.DOER_MAX_WALL_S == 0


def test_the_iteration_tiers_are_the_ones_documented():
    assert _config.MAX_DOER_ITERS == 4
    assert _config.MAX_DOER_ITERS_MODERATE == 20
    assert _config.MAX_DOER_ITERS_COMPLEX == 40
    assert _config.MAX_DOER_ITERS_CAP == 200


def test_a_verbose_plan_does_not_buy_a_huge_budget():
    """The dynamic term counted NUMBERED LINES, so acceptance criteria and
    risks read as subtasks: a trivial ticket with a wordy plan got the 200
    ceiling and the triage verdict stopped mattering."""
    plan = "\n".join(f"{i}. acceptance criterion" for i in range(1, 42))
    state = {"plan_md": plan, "triage_verdict": '{"complexity":"trivial"}'}
    assert _parsers._effective_max_iters(state) <= 60


def test_a_real_decomposition_still_scales():
    """A STRUCTURED plan is a real decomposition and must keep its budget."""
    subs = ", ".join('{"id": "s%d", "goal": "do %d"}' % (i, i)
                     for i in range(12))
    state = {"plan_md": '{"subtickets": [%s]}' % subs,
             "triage_verdict": '{"complexity":"moderate"}'}
    assert _parsers._effective_max_iters(state) >= 60


# ── state that must not survive an iteration ─────────────────────────────


def test_one_incomplete_turn_does_not_poison_the_run():
    """`doer_incomplete` is written when a Doer turn stops early or lands zero
    edits, and the quality gate turns it into a hard fail. Nothing cleared it,
    so a single bad turn made the pass-exit unreachable for the rest of the
    run — the loop then ground to its ceiling with a green tree and a model
    saying "pass" every iteration."""
    state = {"doer_iters": 0, "doer_incomplete": True}
    assert _run(state) == _gates.ROUTE_LOOP
    assert not state.get("doer_incomplete")


def test_the_repeat_guard_does_not_leak_across_iterations():
    """It counts identical (tool, args) calls for the whole RUN. `run_tests`
    with byte-identical args is the NORMAL case once per iteration, so from
    iteration 4 the guard short-circuited it — and the after-tool callback then
    recorded the green suite as tests_ok=False."""
    state = {"doer_iters": 0, "_repeat_counts": {"run_tests|{}": 3}}
    assert _run(state) == _gates.ROUTE_LOOP
    assert not state.get("_repeat_counts")


def test_the_iteration_ceiling_ships_partial_instead_of_buying_a_replan():
    """The ceiling is spent work. Leaving this branch's verdict a plain `fail`
    let the validator replan a loop that had already exhausted itself — the
    whole pipeline again, for the same result."""
    state = {"feedback_verdict": "fail something is wrong"}
    state["doer_iters"] = _parsers._effective_max_iters(state) - 1
    assert _run(state) == _gates.ROUTE_EXIT
    assert "loop_budget_kill" in str(state.get("feedback_verdict"))
    assert "iteration ceiling" in str(state.get("feedback_verdict"))


def test_a_passing_verdict_still_exits_immediately():
    state = {"doer_iters": 0, "feedback_verdict": "pass looks good"}
    assert _run(state) == _gates.ROUTE_EXIT
    assert "loop_budget_kill" not in str(state.get("feedback_verdict"))


def test_the_wall_clock_kill_fires_and_says_so(monkeypatch):
    """No test referenced DOER_MAX_WALL_S or doer_loop_started_at at all."""
    monkeypatch.setattr(_gates, "DOER_MAX_WALL_S", 60)
    import time as _t
    state = {"doer_iters": 1, "doer_loop_started_at": _t.time() - 600}
    assert _run(state) == _gates.ROUTE_EXIT
    assert "wall-clock budget" in str(state.get("feedback_verdict"))
