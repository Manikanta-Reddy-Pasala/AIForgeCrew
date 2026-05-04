"""Cover the Verifier-reject → Planner REPLAN loop in run_ticket.run().

We stub every archetype and the heavyweight side-effects (DB inserts,
learner persistence, allowed-files fetch) so the test exercises pure
control flow.
"""
from __future__ import annotations

from unittest import mock

import pytest

from aiforge_core.orchestrator import run_ticket as rt


class _FakeAgent:
    """Minimal archetype double — captures call count, returns scripted out."""

    def __init__(self, scripted: list[dict]):
        self._scripted = list(scripted)
        self.calls: list[dict] = []
        self.repo = ""
        self.ticket_id = ""

    def run(self, *, ctx):  # noqa: ARG002
        self.calls.append(ctx)
        if self._scripted:
            return self._scripted.pop(0)
        return self._scripted_default()

    def _scripted_default(self) -> dict:
        return {}


def _plan(steps_n: int = 1) -> dict:
    return {
        "artifact_type": "plan",
        "steps": [
            {"id": i + 1, "action": "edit",
             "target": f"src/F{i}.java",
             "expected": "x", "depends_on": []}
            for i in range(steps_n)
        ],
        "expected_token_budget": 500,
    }


def _grounding_ok() -> dict:
    return {
        "artifact_type": "grounding",
        "resolved": True, "unresolved_refs": [],
    }


@pytest.fixture
def patched_run(monkeypatch):
    """Wire up minimal stubs so rt.run() can execute end-to-end."""

    def _no_db(*a, **kw):  # noqa: ARG001
        return False

    def _no_status(*a, **kw):  # noqa: ARG001
        return None

    def _allowed(*a, **kw):  # noqa: ARG001
        return ["src/F0.java", "src/F1.java"]

    monkeypatch.setattr(rt, "_insert_ticket_row", _no_db)
    monkeypatch.setattr(rt, "_update_ticket_status", _no_status)
    monkeypatch.setattr(rt, "_fetch_allowed_files", _allowed)

    # Disable learner DB writes
    from aiforge_core.memory import online_learner as learner

    monkeypatch.setattr(learner, "migrate", lambda: None)
    monkeypatch.setattr(learner, "record_audit", lambda **kw: None)
    monkeypatch.setattr(learner, "top_skills_for", lambda **kw: [])
    monkeypatch.setattr(learner, "top_failures_for", lambda **kw: [])
    monkeypatch.setattr(learner, "record_failure",
                        lambda **kw: None)
    monkeypatch.setattr(learner, "record_episodic",
                        lambda **kw: None)
    monkeypatch.setattr(learner, "record_step_trace",
                        lambda *a, **kw: None)
    monkeypatch.setattr(learner, "update_procedural",
                        lambda **kw: None)
    monkeypatch.setattr(learner, "promote_skill",
                        lambda **kw: None)
    monkeypatch.setattr(learner, "add_attachment",
                        lambda **kw: None)
    monkeypatch.setattr(learner, "attachments_for",
                        lambda *a, **kw: [])

    # Disable workflow short-circuit
    monkeypatch.setattr(rt, "_maybe_run_workflow", lambda **kw: None)
    monkeypatch.setattr(rt, "_maybe_run_trial_balance", lambda **kw: None)
    return monkeypatch


def _build_factory(agents: dict[str, _FakeAgent]):
    def _build(role: str, repo_path=None):  # noqa: ARG001
        return agents[role]

    return _build


def test_verifier_reject_triggers_replan(patched_run):
    """First plan rejected → loop calls Planner again with carried issues."""
    understander = _FakeAgent([{"context_md": "ctx", "key": "v"}])
    # Two planner calls: first attempt, then post-reject re-plan.
    planner = _FakeAgent([_plan(1), _plan(2)])
    grounder = _FakeAgent([_grounding_ok(), _grounding_ok()])
    verifier = _FakeAgent([
        {"verdict": "reject", "issues": [
            {"step_id": 1, "kind": "scope_creep",
             "message": "step touches unrelated file"},
        ]},
        {"verdict": "pass", "issues": []},
    ])
    doer = _FakeAgent([{"applied": False, "udiff": "", "outcome": "ok",
                        "applied_branch": "", "errors": []}] * 5)
    validator = _FakeAgent([{"decision": "approve", "reason": "ok"}] * 5)
    tester = _FakeAgent([{"tests": [], "coverage_target": 0.8}])
    architect = _FakeAgent([{"decision": "approve",
                             "comments": [], "mr_title": "",
                             "mr_body": "", "mr_url": ""}])
    cleaner = _FakeAgent([{}])

    agents = {
        "understander": understander, "planner": planner,
        "grounder": grounder, "verifier": verifier,
        "doer": doer, "validator": validator,
        "tester": tester, "architect": architect,
        "learner": cleaner,
    }
    patched_run.setattr(rt.registry, "build", _build_factory(agents))

    out = rt.run(repo="PosClientBackend", title="t", body="b",
                 apply=False, open_mr=False)

    # Planner ran twice (one reject + one accept).
    assert len(planner.calls) == 2
    # Verifier ran twice — once for reject, once for pass.
    assert len(verifier.calls) == 2
    # Second Planner call carried the verifier issue as an unresolved.
    second_unresolved = planner.calls[1].get("unresolved_refs", [])
    assert any(
        "verifier" in str(u.get("target", "")).lower()
        for u in second_unresolved
    )
    # Final verdict is the second (pass) one.
    assert out["verifier_verdict"]["verdict"] == "pass"


def test_verifier_pass_skips_replan(patched_run):
    """First plan passes verifier → Planner runs only once."""
    understander = _FakeAgent([{"context_md": "ctx"}])
    planner = _FakeAgent([_plan(1)])
    grounder = _FakeAgent([_grounding_ok()])
    verifier = _FakeAgent([{"verdict": "pass", "issues": []}])
    doer = _FakeAgent([{"applied": False, "udiff": "", "outcome": "ok",
                        "applied_branch": "", "errors": []}] * 3)
    validator = _FakeAgent([{"decision": "approve", "reason": "ok"}] * 3)
    tester = _FakeAgent([{"tests": [], "coverage_target": 0.8}])
    architect = _FakeAgent([{"decision": "approve",
                             "comments": [], "mr_title": "",
                             "mr_body": "", "mr_url": ""}])
    cleaner = _FakeAgent([{}])

    agents = {
        "understander": understander, "planner": planner,
        "grounder": grounder, "verifier": verifier,
        "doer": doer, "validator": validator,
        "tester": tester, "architect": architect,
        "learner": cleaner,
    }
    patched_run.setattr(rt.registry, "build", _build_factory(agents))

    out = rt.run(repo="PosClientBackend", title="t", body="b",
                 apply=False, open_mr=False)

    assert len(planner.calls) == 1
    assert len(verifier.calls) == 1
    assert out["verifier_verdict"]["verdict"] == "pass"


def test_repeated_reject_caps_at_three_attempts(patched_run):
    """Even with persistent reject the loop bails after 3 attempts."""
    understander = _FakeAgent([{"context_md": "ctx"}])
    planner = _FakeAgent([_plan(1), _plan(1), _plan(1)])
    grounder = _FakeAgent([_grounding_ok(), _grounding_ok(),
                           _grounding_ok()])
    verifier = _FakeAgent([
        {"verdict": "reject", "issues": [
            {"step_id": 1, "kind": "stuck", "message": "no"},
        ]},
        {"verdict": "reject", "issues": [
            {"step_id": 1, "kind": "stuck", "message": "no"},
        ]},
        {"verdict": "reject", "issues": [
            {"step_id": 1, "kind": "stuck", "message": "no"},
        ]},
    ])
    doer = _FakeAgent([{"applied": False, "udiff": "", "outcome": "ok",
                        "applied_branch": "", "errors": []}] * 3)
    validator = _FakeAgent([{"decision": "approve", "reason": "ok"}] * 3)
    tester = _FakeAgent([{"tests": [], "coverage_target": 0.8}])
    architect = _FakeAgent([{"decision": "approve",
                             "comments": [], "mr_title": "",
                             "mr_body": "", "mr_url": ""}])
    cleaner = _FakeAgent([{}])

    agents = {
        "understander": understander, "planner": planner,
        "grounder": grounder, "verifier": verifier,
        "doer": doer, "validator": validator,
        "tester": tester, "architect": architect,
        "learner": cleaner,
    }
    patched_run.setattr(rt.registry, "build", _build_factory(agents))

    out = rt.run(repo="PosClientBackend", title="t", body="b",
                 apply=False, open_mr=False)

    # Loop hard-cap is 3 plan_attempts.
    assert len(planner.calls) == 3
    assert len(verifier.calls) == 3
    # Final verdict still reject — orchestrator records but doesn't crash.
    assert out["verifier_verdict"]["verdict"] == "reject"
