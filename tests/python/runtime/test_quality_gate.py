"""Unit tests for the deterministic quality gate (gap A1).

The gate is a pure function over typecheck/test/lint signals. A red
typecheck OR red tests forces ``gate=fail``; lint is warn-only; ``None``
signals (not-run) never fail the gate. ``gate_verdict`` downgrades a
model ``pass`` to ``fail`` when the gate fails, but never touches
``scope_violation`` (scope outranks test colour).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from aiforge_core.runtime import quality_gate


# --- evaluate() -----------------------------------------------------------

def test_evaluate_all_none_passes():
    g = quality_gate.evaluate(typecheck_ok=None, tests_ok=None)
    assert g["gate"] == "pass"
    assert g["reasons"] == []


def test_evaluate_all_true_passes():
    g = quality_gate.evaluate(typecheck_ok=True, tests_ok=True, lint_ok=True)
    assert g["gate"] == "pass"
    assert g["reasons"] == []


def test_evaluate_tests_red_fails():
    g = quality_gate.evaluate(typecheck_ok=True, tests_ok=False)
    assert g["gate"] == "fail"
    assert any("test" in r.lower() for r in g["reasons"])


def test_evaluate_typecheck_red_fails():
    g = quality_gate.evaluate(typecheck_ok=False, tests_ok=True)
    assert g["gate"] == "fail"
    assert any("typecheck" in r.lower() for r in g["reasons"])


def test_evaluate_both_red_lists_both_reasons():
    g = quality_gate.evaluate(typecheck_ok=False, tests_ok=False)
    assert g["gate"] == "fail"
    assert len(g["reasons"]) == 2


def test_evaluate_lint_red_is_warn_only():
    g = quality_gate.evaluate(typecheck_ok=True, tests_ok=True, lint_ok=False)
    assert g["gate"] == "pass"
    # lint failure surfaces as a soft reason but does not fail the gate.
    assert any("lint" in r.lower() for r in g["reasons"])


def test_evaluate_lint_red_alone_does_not_fail():
    g = quality_gate.evaluate(typecheck_ok=None, tests_ok=None, lint_ok=False)
    assert g["gate"] == "pass"


# --- gate_verdict() -------------------------------------------------------

def test_gate_verdict_downgrades_pass_when_gate_fails():
    gate = {"gate": "fail", "reasons": ["tests red"]}
    assert quality_gate.gate_verdict("pass", gate) == "fail"


def test_gate_verdict_keeps_pass_when_gate_passes():
    gate = {"gate": "pass", "reasons": []}
    assert quality_gate.gate_verdict("pass", gate) == "pass"


def test_gate_verdict_preserves_scope_violation():
    gate = {"gate": "fail", "reasons": ["tests red"]}
    assert quality_gate.gate_verdict("scope_violation", gate) == "scope_violation"


def test_gate_verdict_none_signals_no_downgrade():
    # all-None gate passes → model pass stays pass.
    gate = quality_gate.evaluate(typecheck_ok=None, tests_ok=None)
    assert quality_gate.gate_verdict("pass", gate) == "pass"


def test_gate_verdict_fail_stays_fail():
    gate = {"gate": "fail", "reasons": ["tests red"]}
    assert quality_gate.gate_verdict("fail", gate) == "fail"


# --- feedback wiring ------------------------------------------------------

def test_feedback_callback_downgrades_pass_on_red_tests():
    """The feedback after-callback rewrites a model ``pass`` to ``fail``
    when ``tests_ok=False`` lives in callback state."""
    from aiforge_core.agents import feedback

    cb = feedback.make_quality_gate_after_callback()
    assert cb is not None

    ctx = MagicMock()
    ctx.state = {"feedback_verdict": "pass", "tests_ok": False}

    import asyncio
    asyncio.run(cb(callback_context=ctx))

    assert ctx.state["feedback_verdict"] == "fail"


def test_feedback_callback_noop_when_signals_absent():
    """No typecheck/test signal in state → verdict untouched."""
    from aiforge_core.agents import feedback

    cb = feedback.make_quality_gate_after_callback()
    ctx = MagicMock()
    ctx.state = {"feedback_verdict": "pass"}

    import asyncio
    asyncio.run(cb(callback_context=ctx))

    assert ctx.state["feedback_verdict"] == "pass"
