"""Fix 1 — quality signals (tests_ok/typecheck_ok/lint_ok) must be cleared
between Doer loop iterations so a stale green from iter-1 can't let a
regressed iter-2 (that never re-ran the tools) sail through the Feedback
gate. They must be PRESERVED on the exit branch (the Validator reads them)."""
from __future__ import annotations

import asyncio

from aiforge_core.runtime import graph_pipeline as gp


class _FakeCtx:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.route = None


def _run(coro):
    return asyncio.run(coro)


def test_loop_branch_clears_quality_signals() -> None:
    # Failing feedback + iters below cap → routes LOOP → signals cleared.
    state = {
        "feedback_verdict": '{"verdict": "fail"}',
        "tests_ok": True,
        "typecheck_ok": True,
        "lint_ok": True,
    }
    ctx = _FakeCtx(state)
    _run(gp._loop_gate(ctx))
    assert ctx.route == gp.ROUTE_LOOP
    assert state.get("tests_ok") is None
    assert state.get("typecheck_ok") is None
    assert state.get("lint_ok") is None


def test_exit_branch_preserves_quality_signals_on_pass() -> None:
    # Passing feedback → routes EXIT → signals preserved for the Validator.
    state = {
        "feedback_verdict": '{"verdict": "pass"}',
        "tests_ok": True,
        "typecheck_ok": True,
        "lint_ok": True,
    }
    ctx = _FakeCtx(state)
    _run(gp._loop_gate(ctx))
    assert ctx.route == gp.ROUTE_EXIT
    assert state.get("tests_ok") is True
    assert state.get("typecheck_ok") is True
    assert state.get("lint_ok") is True


def test_exit_branch_preserves_quality_signals_at_cap() -> None:
    # iters >= MAX_DOER_ITERS → EXIT even with a failing verdict → preserve.
    # 'low' pins the base-tier cap (unset complexity defaults to 'moderate' →
    # 20 iters under the dynamic tiered budget).
    state = {
        "feedback_verdict": '{"verdict": "fail"}',
        "doer_iters": gp.MAX_DOER_ITERS - 1,
        "tests_ok": False,
        "complexity": "low",
    }
    ctx = _FakeCtx(state)
    _run(gp._loop_gate(ctx))
    assert ctx.route == gp.ROUTE_EXIT
    assert state.get("tests_ok") is False
