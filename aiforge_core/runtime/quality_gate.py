"""Deterministic quality gate (gap A1).

The Feedback agent judges by model reasoning alone; that lets a confident
model wave through a PR while the type-checker or test suite is red. This
module adds a *hard* gate: a red typecheck OR red tests forces the verdict
to ``fail`` regardless of what the model said.

KISS — two pure functions, no I/O:

  * :func:`evaluate` turns the three boolean signals into a
    ``{"gate": "pass"|"fail", "reasons": [...]}`` dict.
  * :func:`gate_verdict` downgrades a model ``pass`` to ``fail`` when the
    gate failed, while leaving ``scope_violation`` untouched (scope
    outranks test colour, matching the Feedback YAML rule).

Signal semantics (per signal):
  * ``True``  — the check ran and was green.
  * ``False`` — the check ran and was red → fails the gate (typecheck /
    tests) or warns (lint).
  * ``None``  — the check did not run / unknown → never fails the gate.

This keeps the gate backward-compatible: when a run carries no
typecheck/test signals (the common case today) every signal is ``None``,
the gate passes, and the model verdict flows through unchanged.
"""
from __future__ import annotations

import os
from typing import Any

__all__ = ["evaluate", "gate_verdict", "make_quality_signal_callback",
           "mark_test_gaming"]

# Doer tool name → session-state signal key the gate reads.
_TOOL_SIGNAL_KEYS = {
    "run_tests": "tests_ok",
    "typecheck": "typecheck_ok",
    "format": "lint_ok",
}


def make_quality_signal_callback():
    """Return an ADK ``after_tool_callback`` that records quality signals.

    The gate (``evaluate``/``gate_verdict`` below) reads ``tests_ok`` /
    ``typecheck_ok`` / ``lint_ok`` from session state — but nothing
    wrote them, so the gate was permanently pass. This callback watches
    the Doer's run_tests / typecheck / format tool results and writes
    ``result["ok"]`` into the matching key. Returns ``None`` — the tool
    response is left alone — except when the no-progress rule trips (see
    :mod:`aiforge_core.runtime.doer_no_progress`).
    """
    from aiforge_core.runtime.doer_no_progress import DoerProgressGuard
    guard = DoerProgressGuard()

    def _cb(*, tool, args, tool_context, tool_response, **_kw):
        _record(tool, tool_context, tool_response)
        # The no-progress rule: the only time this callback replaces the
        # response is to put the loop guard's note in front of the model.
        try:
            state = getattr(tool_context, "state", None)
            run_key = getattr(tool_context, "invocation_id", None) or id(state)
            return guard.step(run_key, getattr(tool, "name", "") or "", args,
                              tool_response, state)
        except Exception:  # noqa: BLE001 — a guard must never break a call
            return None

    def _record(tool, tool_context, tool_response):
        try:
            name = getattr(tool, "name", "") or ""
            key = _TOOL_SIGNAL_KEYS.get(name)
            if not key or not isinstance(tool_response, dict):
                return None
            ok = tool_response.get("ok")
            if isinstance(ok, bool):
                state = getattr(tool_context, "state", None)
                if state is not None:
                    state[key] = ok
                    # What the run failed on, for the Doer loop's
                    # same-failure rule; a later green run clears it.
                    fail = test_failure(tool_response) if key == "tests_ok" else []
                    if fail:
                        state["_iter_fail"] = fail
                    elif key == "tests_ok" and ok and state.get("_iter_fail"):
                        state["_iter_fail"] = []
        except Exception:  # noqa: BLE001 — signals are best-effort
            pass
        return None

    return _cb


def test_failure(result) -> list:
    """What a test run failed on, for the Doer loop's same-failure rule:
    ``[signature, count, headline]``, or ``[]`` for a pass or an output that
    names no failure."""
    try:
        if not isinstance(result, dict) or result.get("ok") is not False:
            return []
        from aiforge_core.runtime.failure_signature import failure_of, result_text
        fail = failure_of(result_text(result))
        return list(fail) if fail.signature else []
    except Exception:  # noqa: BLE001 — signals are best-effort
        return []


def _strict_test_gate() -> bool:
    """``AIFORGE_STRICT_TEST_GATE`` — gate the (riskier) "tests declared but
    never ran" downgrade. Default OFF to avoid false-negatives on trivial
    tasks that legitimately run no tests."""
    return os.environ.get("AIFORGE_STRICT_TEST_GATE", "0").strip().lower() in {
        "1", "true", "yes", "on"}


def evaluate(
    *,
    typecheck_ok: bool | None,
    tests_ok: bool | None,
    lint_ok: bool | None = None,
    doer_incomplete: bool | None = None,
    tests_declared: bool | None = None,
) -> dict[str, Any]:
    """Combine the quality signals into a gate decision.

    Args:
        typecheck_ok: type-check result. ``False`` → hard fail.
        tests_ok: test-suite result. ``False`` → hard fail.
        lint_ok: lint result. ``False`` → soft warning only (never fails
            the gate).
        doer_incomplete: the Doer stopped WITHOUT finishing (hit the runaway
            safety cap / turn deadline — a ``"(stopped: ..."`` banner). This
            is unambiguous, so ``True`` → hard fail regardless of the flag
            (Fix 3a): a capped run must never ship an optimistic ``pass``.
        tests_declared: the plan/acceptance declared a test bar. When set and
            ``tests_ok is None`` (tests never ran), the gate hard-fails ONLY
            if ``AIFORGE_STRICT_TEST_GATE`` is on — kept behind the flag
            because a trivial task may legitimately run no tests.

    Returns:
        ``{"gate": "pass"|"fail", "reasons": [...]}``. ``reasons`` carries
        a short human string for every signal that contributed — hard
        failures and the soft lint warning alike — so the caller can log
        why a verdict was downgraded.
    """
    reasons: list[str] = []
    failed = False

    if typecheck_ok is False:
        reasons.append("typecheck failed")
        failed = True
    if tests_ok is False:
        reasons.append("tests failed")
        failed = True
    if doer_incomplete:
        # Unambiguous: the Doer ran out of steps/deadline mid-task.
        reasons.append("doer stopped incomplete (hit cap/deadline)")
        failed = True
    if tests_declared and tests_ok is None and _strict_test_gate():
        reasons.append("tests declared but never ran (strict gate)")
        failed = True
    if lint_ok is False:
        # Soft signal — recorded for visibility, does not fail the gate.
        reasons.append("lint failed (warn only)")

    return {"gate": "fail" if failed else "pass", "reasons": reasons}


def gate_verdict(model_verdict: str, gate: dict[str, Any]) -> str:
    """Reconcile the model's verdict with the hard gate.

    Args:
        model_verdict: the Feedback agent's token —
            ``pass`` | ``fail`` | ``scope_violation``.
        gate: the dict returned by :func:`evaluate`.

    Returns:
        ``scope_violation`` unchanged (scope outranks the gate). A model
        ``pass`` becomes ``fail`` when ``gate["gate"] == "fail"``. Every
        other combination returns ``model_verdict`` untouched.
    """
    verdict = (model_verdict or "").strip().lower()
    if verdict == "scope_violation":
        return model_verdict
    if gate.get("gate") == "fail" and verdict == "pass":
        return "fail"
    return model_verdict


def mark_test_gaming(state, repo_root: str) -> bool:
    """A Doer pass the Feedback judged ``pass``: do its edits make the tests
    pass by DETECTING the test (:mod:`aiforge_core.runtime.gaming_check`)?
    Only the run's own lines count: ``state['gaming_base']`` is the snapshot
    taken when the run started (adk_runner ``_ticket_state``).
    On a hit the verdict becomes ``partial test_gaming: <evidence>`` — shipped
    for review with the evidence, never as a success — and
    ``state['quality_issue']`` / ``state['test_gaming_evidence']`` carry it.
    Returns True on a hit. Best-effort: an error is no hit."""
    if not repo_root:
        return False
    try:
        from aiforge_core.runtime import gaming_check as test_gaming
        evidence = test_gaming.check(repo_root,
                                     state.get("gaming_base") or None)
    except Exception:  # noqa: BLE001
        return False
    if not evidence:
        return False
    state["quality_issue"] = "test_gaming"
    state["test_gaming_evidence"] = evidence
    state["feedback_verdict"] = (
        "partial test_gaming: the tests pass because the code detects the "
        "test, not because the behaviour was fixed — " + "; ".join(evidence))
    return True
