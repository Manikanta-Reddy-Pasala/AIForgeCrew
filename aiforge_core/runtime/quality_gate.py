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

from typing import Any

__all__ = ["evaluate", "gate_verdict", "make_quality_signal_callback"]

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
    ``result["ok"]`` into the matching key. Always returns ``None`` so
    the tool response itself is never altered.
    """
    async def _cb(*, tool, args, tool_context, tool_response, **_kw):
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
        except Exception:  # noqa: BLE001 — signals are best-effort
            pass
        return None

    return _cb


def evaluate(
    *,
    typecheck_ok: bool | None,
    tests_ok: bool | None,
    lint_ok: bool | None = None,
) -> dict[str, Any]:
    """Combine the quality signals into a gate decision.

    Args:
        typecheck_ok: type-check result. ``False`` → hard fail.
        tests_ok: test-suite result. ``False`` → hard fail.
        lint_ok: lint result. ``False`` → soft warning only (never fails
            the gate).

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
