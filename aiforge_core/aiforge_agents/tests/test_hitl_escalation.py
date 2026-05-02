"""HITL escalation (#8) — _hitl_reason classifier + _persist_hitl_request."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aiforge_core.aiforge_agents.orchestrator import run_ticket as rt
from aiforge_core.aiforge_agents.learner import online as learner


def _ok_breakers():
    return SimpleNamespace(tripped=False, state=SimpleNamespace(reason=""))


def _tripped_breakers():
    return SimpleNamespace(tripped=True,
                           state=SimpleNamespace(reason="too_many_panic"))


# ─────────── Classifier ────────────────────────────────────────────────

def test_no_escalation_when_clean():
    esc, head, ev = rt._hitl_reason(
        breakers=_ok_breakers(),
        verdict={"verdict": "pass"},
        grounding={"resolved": True},
        validation={"decision": "approve"},
        review={"decision": "approve"},
        plan_attempts=1,
        doer_attempts=1,
        test_run={"ok": True, "framework": "pytest", "passed": 5,
                  "failed": 0},
    )
    assert esc is False
    assert ev == []


def test_breaker_trip_escalates():
    esc, head, ev = rt._hitl_reason(
        breakers=_tripped_breakers(),
        verdict={"verdict": "pass"}, grounding={"resolved": True},
        validation={"decision": "approve"},
        review={"decision": "approve"},
        plan_attempts=1, doer_attempts=1, test_run=None,
    )
    assert esc is True
    assert head == "circuit_breaker"
    assert "Circuit breaker tripped" in ev[0]


def test_persistent_verifier_reject_escalates():
    esc, head, ev = rt._hitl_reason(
        breakers=_ok_breakers(),
        verdict={"verdict": "reject"}, grounding={"resolved": True},
        validation={"decision": "approve"},
        review={"decision": "approve"},
        plan_attempts=3, doer_attempts=1, test_run=None,
    )
    assert esc is True
    assert head == "verifier_persistent_reject"


def test_grounder_unresolved_escalates():
    esc, head, ev = rt._hitl_reason(
        breakers=_ok_breakers(),
        verdict={"verdict": "pass"},
        grounding={"resolved": False, "unresolved_refs": [
            {"target": "X"}, {"target": "Y"},
        ]},
        validation={"decision": "skip"},
        review={"decision": "approve"},
        plan_attempts=2, doer_attempts=1, test_run=None,
    )
    assert esc is True
    assert head == "grounder_unresolved"
    assert "2 reference" in ev[0]


def test_validator_persistent_block_escalates():
    esc, head, _ = rt._hitl_reason(
        breakers=_ok_breakers(),
        verdict={"verdict": "pass"}, grounding={"resolved": True},
        validation={"decision": "block", "reason": "lint_errors"},
        review={"decision": "approve"},
        plan_attempts=1, doer_attempts=3, test_run=None,
    )
    assert esc is True
    assert head == "validator_persistent_block"


def test_validator_block_with_few_attempts_no_escalate():
    """One block isn't enough — wait for the CRITIC retries to exhaust."""
    esc, _, _ = rt._hitl_reason(
        breakers=_ok_breakers(),
        verdict={"verdict": "pass"}, grounding={"resolved": True},
        validation={"decision": "block", "reason": "x"},
        review={"decision": "approve"},
        plan_attempts=1, doer_attempts=1, test_run=None,
    )
    assert esc is False


def test_architect_reject_escalates():
    esc, head, _ = rt._hitl_reason(
        breakers=_ok_breakers(),
        verdict={"verdict": "pass"}, grounding={"resolved": True},
        validation={"decision": "approve"},
        review={"decision": "reject", "comments": ["unsafe pattern"]},
        plan_attempts=1, doer_attempts=1, test_run=None,
    )
    assert esc is True
    assert head == "architect_reject"


def test_test_failure_escalates_even_when_diff_approved():
    esc, head, _ = rt._hitl_reason(
        breakers=_ok_breakers(),
        verdict={"verdict": "pass"}, grounding={"resolved": True},
        validation={"decision": "approve"},
        review={"decision": "approve"},
        plan_attempts=1, doer_attempts=1,
        test_run={"ok": False, "framework": "pytest", "passed": 0,
                  "failed": 3, "timed_out": False},
    )
    assert esc is True
    assert head == "tests_failed"


def test_priority_breaker_first():
    """Multiple triggers — circuit_breaker takes the headline."""
    esc, head, ev = rt._hitl_reason(
        breakers=_tripped_breakers(),
        verdict={"verdict": "reject"}, grounding={"resolved": True},
        validation={"decision": "approve"},
        review={"decision": "reject", "comments": ["x"]},
        plan_attempts=3, doer_attempts=1, test_run=None,
    )
    assert esc is True
    assert head == "circuit_breaker"
    # All evidence still surfaces
    assert len(ev) >= 2


# ─────────── Persistence ───────────────────────────────────────────────

def test_persist_writes_markdown_with_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_RUNS_DIR", str(tmp_path))
    captured: list[dict] = []
    monkeypatch.setattr(learner, "add_attachment",
                        lambda **kw: captured.append(kw) or True)

    class _Log:
        def info(self, *a, **kw):
            pass

    out = rt._persist_hitl_request(
        ticket_id="T-HITL",
        headline="architect_reject",
        evidence=["Architect rejected: unsafe pattern"],
        plan={"steps": [{"id": 1}, {"id": 2}]},
        review={"decision": "reject", "comments": ["unsafe pattern",
                                                   "needs unit test"]},
        validation={"decision": "approve"},
        test_run={"framework": "pytest", "passed": 5, "failed": 1,
                  "ok": False},
        log=_Log(),
    )
    p = Path(out)
    assert p.is_file()
    txt = p.read_text()
    assert "architect_reject" in txt
    assert "Architect rejected" in txt
    assert "test run:" in txt.lower() or "test run: framework" in txt
    assert "## Architect comments" in txt
    assert captured[0]["role"] == "hitl_request"


def test_persist_handles_io_failure(monkeypatch):
    monkeypatch.setenv("AIFORGE_RUNS_DIR", "/no/such/path/that/exists")

    class _Log:
        def info(self, *a, **kw):
            pass

    # Patch Path.mkdir to raise
    import pathlib

    def boom(self, *a, **kw):
        raise OSError("readonly fs")

    monkeypatch.setattr(pathlib.Path, "mkdir", boom)

    out = rt._persist_hitl_request(
        ticket_id="T", headline="x", evidence=["e"],
        plan={}, review={}, validation={},
        test_run=None, log=_Log(),
    )
    assert out == ""
