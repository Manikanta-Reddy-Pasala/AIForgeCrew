"""Cover the CRITIC retry cap (#5).

Two behaviours under test:
  1. Cap honoured — Doer is invoked AIFORGE_CRITIC_MAX times when
     Validator keeps blocking AND the udiff/problem set keeps changing.
  2. Diminishing-returns guard — when attempt N's udiff is identical to
     attempt N-1's OR problem count grew, the loop bails early.
"""
from __future__ import annotations

import pytest

from aiforge_core.aiforge_agents.orchestrator import run_ticket as rt


class _FakeAgent:
    def __init__(self, scripted: list[dict]):
        self._scripted = list(scripted)
        self.calls: list[dict] = []
        self.repo = ""
        self.ticket_id = ""

    def run(self, *, ctx):
        self.calls.append(ctx)
        if self._scripted:
            return self._scripted.pop(0)
        return {}


def _plan() -> dict:
    return {
        "artifact_type": "plan",
        "steps": [
            {"id": 1, "action": "edit", "target": "src/F0.java",
             "expected": "x", "depends_on": []},
        ],
    }


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(rt, "_insert_ticket_row", lambda *a, **kw: False)
    monkeypatch.setattr(rt, "_update_ticket_status", lambda *a, **kw: None)
    monkeypatch.setattr(rt, "_fetch_allowed_files",
                        lambda **kw: ["src/F0.java"])
    monkeypatch.setattr(rt, "_maybe_run_workflow", lambda **kw: None)
    monkeypatch.setattr(rt, "_maybe_run_trial_balance", lambda **kw: None)
    monkeypatch.setattr(rt, "_resolve_repo_path", lambda r: "")
    monkeypatch.setattr(rt, "_git_head_sha", lambda *a, **kw: "")
    monkeypatch.setattr(rt, "_git_reset_to", lambda *a, **kw: True)

    from aiforge_core.aiforge_agents.learner import online as learner

    for fn in ("migrate", "record_audit", "record_failure",
               "record_episodic", "record_step_trace",
               "update_procedural", "promote_skill",
               "add_attachment"):
        monkeypatch.setattr(learner, fn, lambda *a, **kw: None)
    monkeypatch.setattr(learner, "top_skills_for", lambda **kw: [])
    monkeypatch.setattr(learner, "top_failures_for", lambda **kw: [])
    monkeypatch.setattr(learner, "attachments_for", lambda *a, **kw: [])
    return monkeypatch


def test_critic_runs_three_attempts_when_progress(stubbed):
    """Each retry produces a different udiff with shrinking problems →
    full cap of 3 attempts is consumed."""
    stubbed.setenv("AIFORGE_CRITIC_MAX", "3")

    doer = _FakeAgent([
        {"applied": False, "udiff": "diff-A",
         "problems": [1, 2, 3, 4],
         "applied_branch": "", "step_id": 1, "target": "src/F0.java"},
        {"applied": False, "udiff": "diff-B",
         "problems": [1, 2, 3],
         "applied_branch": "", "step_id": 1, "target": "src/F0.java"},
        {"applied": False, "udiff": "diff-C",
         "problems": [1, 2],
         "applied_branch": "", "step_id": 1, "target": "src/F0.java"},
    ])
    validator = _FakeAgent([
        {"decision": "block", "reason": "linter"},
        {"decision": "block", "reason": "linter"},
        {"decision": "block", "reason": "linter"},
    ])
    agents = {
        "understander": _FakeAgent([{"context_md": "ctx"}]),
        "planner": _FakeAgent([_plan()]),
        "grounder": _FakeAgent([{"resolved": True, "unresolved_refs": []}]),
        "verifier": _FakeAgent([{"verdict": "pass", "issues": []}]),
        "doer": doer, "validator": validator,
        "tester": _FakeAgent([{"tests": [], "coverage_target": 0.8}]),
        "architect": _FakeAgent([{"decision": "approve", "comments": [],
                                  "mr_title": "", "mr_body": "",
                                  "mr_url": ""}]),
        "learner": _FakeAgent([{}]),
    }
    stubbed.setattr(rt.registry, "build",
                    lambda role, repo_path=None: agents[role])

    rt.run(repo="PosClientBackend", title="t", body="b",
           apply=False, open_mr=False, ticket_id="T1")

    assert len(doer.calls) == 3
    assert len(validator.calls) == 3


def test_critic_bails_when_udiff_identical(stubbed):
    """Same udiff twice in a row → bail after attempt 2 even with cap=3."""
    stubbed.setenv("AIFORGE_CRITIC_MAX", "3")

    doer = _FakeAgent([
        {"applied": False, "udiff": "diff-X",
         "problems": [1, 2],
         "applied_branch": "", "step_id": 1, "target": "src/F0.java"},
        {"applied": False, "udiff": "diff-X",   # identical → bail
         "problems": [1, 2],
         "applied_branch": "", "step_id": 1, "target": "src/F0.java"},
    ])
    validator = _FakeAgent([
        {"decision": "block", "reason": "linter"},
        {"decision": "block", "reason": "linter"},
    ])
    agents = {
        "understander": _FakeAgent([{"context_md": "ctx"}]),
        "planner": _FakeAgent([_plan()]),
        "grounder": _FakeAgent([{"resolved": True, "unresolved_refs": []}]),
        "verifier": _FakeAgent([{"verdict": "pass", "issues": []}]),
        "doer": doer, "validator": validator,
        "tester": _FakeAgent([{"tests": [], "coverage_target": 0.8}]),
        "architect": _FakeAgent([{"decision": "approve", "comments": [],
                                  "mr_title": "", "mr_body": "",
                                  "mr_url": ""}]),
        "learner": _FakeAgent([{}]),
    }
    stubbed.setattr(rt.registry, "build",
                    lambda role, repo_path=None: agents[role])

    rt.run(repo="PosClientBackend", title="t", body="b",
           apply=False, open_mr=False, ticket_id="T1")

    # Doer runs twice, then bails — third attempt skipped.
    assert len(doer.calls) == 2


def test_critic_bails_when_problems_grow(stubbed):
    """Problem count grew between attempts → bail after attempt 2."""
    stubbed.setenv("AIFORGE_CRITIC_MAX", "3")

    doer = _FakeAgent([
        {"applied": False, "udiff": "diff-A",
         "problems": [1, 2],
         "applied_branch": "", "step_id": 1, "target": "src/F0.java"},
        {"applied": False, "udiff": "diff-B",
         "problems": [1, 2, 3, 4, 5],   # grew → bail
         "applied_branch": "", "step_id": 1, "target": "src/F0.java"},
    ])
    validator = _FakeAgent([
        {"decision": "block", "reason": "linter"},
        {"decision": "block", "reason": "linter"},
    ])
    agents = {
        "understander": _FakeAgent([{"context_md": "ctx"}]),
        "planner": _FakeAgent([_plan()]),
        "grounder": _FakeAgent([{"resolved": True, "unresolved_refs": []}]),
        "verifier": _FakeAgent([{"verdict": "pass", "issues": []}]),
        "doer": doer, "validator": validator,
        "tester": _FakeAgent([{"tests": [], "coverage_target": 0.8}]),
        "architect": _FakeAgent([{"decision": "approve", "comments": [],
                                  "mr_title": "", "mr_body": "",
                                  "mr_url": ""}]),
        "learner": _FakeAgent([{}]),
    }
    stubbed.setattr(rt.registry, "build",
                    lambda role, repo_path=None: agents[role])

    rt.run(repo="PosClientBackend", title="t", body="b",
           apply=False, open_mr=False, ticket_id="T1")

    assert len(doer.calls) == 2


def test_critic_cap_env_override(stubbed):
    """AIFORGE_CRITIC_MAX=1 → no retries at all, just first attempt."""
    stubbed.setenv("AIFORGE_CRITIC_MAX", "1")

    doer = _FakeAgent([
        {"applied": False, "udiff": "diff-Z",
         "problems": [1],
         "applied_branch": "", "step_id": 1, "target": "src/F0.java"},
    ])
    validator = _FakeAgent([
        {"decision": "block", "reason": "linter"},
    ])
    agents = {
        "understander": _FakeAgent([{"context_md": "ctx"}]),
        "planner": _FakeAgent([_plan()]),
        "grounder": _FakeAgent([{"resolved": True, "unresolved_refs": []}]),
        "verifier": _FakeAgent([{"verdict": "pass", "issues": []}]),
        "doer": doer, "validator": validator,
        "tester": _FakeAgent([{"tests": [], "coverage_target": 0.8}]),
        "architect": _FakeAgent([{"decision": "approve", "comments": [],
                                  "mr_title": "", "mr_body": "",
                                  "mr_url": ""}]),
        "learner": _FakeAgent([{}]),
    }
    stubbed.setattr(rt.registry, "build",
                    lambda role, repo_path=None: agents[role])

    rt.run(repo="PosClientBackend", title="t", body="b",
           apply=False, open_mr=False, ticket_id="T1")

    assert len(doer.calls) == 1
