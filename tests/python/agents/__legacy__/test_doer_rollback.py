"""Cover the per-step rollback in the multi-step Doer loop.

Two layers:
  1. Unit-level: _git_head_sha + _git_reset_to against a real temp repo.
  2. Integration: stubbed agents drive the orchestrator; we monkey-patch
     the rollback helper to record invocations.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aiforge_core.orchestrator import run_ticket as rt


# ─────────── Helpers ───────────────────────────────────────────────────

def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd,
                          capture_output=True, text=True)


@pytest.fixture
def tmp_repo(tmp_path) -> str:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(str(repo), "init", "-q")
    _git(str(repo), "config", "user.email", "x@y")
    _git(str(repo), "config", "user.name", "x")
    (repo / "a.txt").write_text("base\n")
    _git(str(repo), "add", "-A")
    _git(str(repo), "commit", "-q", "-m", "base")
    return str(repo)


# ─────────── Helper unit tests ─────────────────────────────────────────

def test_head_sha_reads_current(tmp_repo):
    sha = rt._git_head_sha(tmp_repo, branch="main")
    assert sha and len(sha) == 40


def test_head_sha_missing_repo():
    assert rt._git_head_sha("/no/such/path", branch="main") == ""
    assert rt._git_head_sha("", branch="main") == ""


def test_reset_to_drops_commit(tmp_repo):
    base_sha = rt._git_head_sha(tmp_repo, branch="main")
    (Path(tmp_repo) / "b.txt").write_text("change\n")
    _git(tmp_repo, "add", "-A")
    _git(tmp_repo, "commit", "-q", "-m", "step")
    new_sha = rt._git_head_sha(tmp_repo, branch="main")
    assert new_sha != base_sha
    assert rt._git_reset_to(tmp_repo, base_sha) is True
    assert rt._git_head_sha(tmp_repo, branch="main") == base_sha
    # b.txt should be gone after hard reset.
    assert not (Path(tmp_repo) / "b.txt").exists()


def test_reset_to_empty_sha_is_safe(tmp_repo):
    """Defensive: empty sha must NOT reset (would lose work)."""
    sha = rt._git_head_sha(tmp_repo, branch="main")
    assert rt._git_reset_to(tmp_repo, "") is False
    assert rt._git_head_sha(tmp_repo, branch="main") == sha


# ─────────── Orchestrator integration ──────────────────────────────────

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
        "expected_token_budget": 500,
    }


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(rt, "_insert_ticket_row", lambda *a, **kw: False)
    monkeypatch.setattr(rt, "_update_ticket_status", lambda *a, **kw: None)
    monkeypatch.setattr(rt, "_fetch_allowed_files",
                        lambda **kw: ["src/F0.java"])
    monkeypatch.setattr(rt, "_maybe_run_workflow", lambda **kw: None)
    monkeypatch.setattr(rt, "_maybe_run_trial_balance", lambda **kw: None)

    from aiforge_core.memory import online_learner as learner

    for fn in ("migrate", "record_audit", "top_skills_for",
               "top_failures_for", "record_failure", "record_episodic",
               "record_step_trace", "update_procedural", "promote_skill",
               "add_attachment", "attachments_for"):
        monkeypatch.setattr(learner, fn, lambda *a, **kw: None)
    monkeypatch.setattr(learner, "top_skills_for", lambda **kw: [])
    monkeypatch.setattr(learner, "top_failures_for", lambda **kw: [])
    monkeypatch.setattr(learner, "attachments_for", lambda *a, **kw: [])

    return monkeypatch


def test_block_after_critic_triggers_rollback(stubbed, tmp_repo):
    """Step's CRITIC retries exhaust + applied=True → rollback fires."""
    head_before = rt._git_head_sha(tmp_repo, branch="main")
    # Simulate a step commit landing on the branch.
    (Path(tmp_repo) / "b.txt").write_text("step\n")
    _git(tmp_repo, "add", "-A")
    _git(tmp_repo, "commit", "-q", "-m", "step1")
    head_after = rt._git_head_sha(tmp_repo, branch="main")
    assert head_after != head_before

    reset_calls: list[tuple[str, str]] = []
    head_calls: list[tuple[str, str]] = []

    def fake_head(repo_path, branch):
        head_calls.append((repo_path, branch))
        # First call (head_before_step) → head_before
        # Subsequent calls (during rollback assertion) → head_after
        if len(head_calls) == 1:
            return head_before
        return head_after

    def fake_reset(repo_path, sha):
        reset_calls.append((repo_path, sha))
        return True

    stubbed.setattr(rt, "_git_head_sha", fake_head)
    stubbed.setattr(rt, "_git_reset_to", fake_reset)
    stubbed.setattr(rt, "_resolve_repo_path", lambda r: tmp_repo)

    understander = _FakeAgent([{"context_md": "ctx"}])
    planner = _FakeAgent([_plan()])
    grounder = _FakeAgent([{"resolved": True, "unresolved_refs": []}])
    verifier = _FakeAgent([{"verdict": "pass", "issues": []}])
    # Doer always says applied=True; Validator always blocks.
    doer = _FakeAgent([
        {"applied": True, "udiff": "diff", "outcome": "ok",
         "applied_branch": "aiforge/T1", "step_id": 1,
         "target": "src/F0.java", "problems": []},
        {"applied": True, "udiff": "diff", "outcome": "ok",
         "applied_branch": "aiforge/T1", "step_id": 1,
         "target": "src/F0.java", "problems": []},
    ])
    validator = _FakeAgent([
        {"decision": "block", "reason": "linter"},
        {"decision": "block", "reason": "linter"},
    ])
    tester = _FakeAgent([{"tests": [], "coverage_target": 0.8}])
    architect = _FakeAgent([{"decision": "approve", "comments": [],
                             "mr_title": "", "mr_body": "", "mr_url": ""}])
    cleaner = _FakeAgent([{}])

    agents = {
        "understander": understander, "planner": planner,
        "grounder": grounder, "verifier": verifier,
        "doer": doer, "validator": validator,
        "tester": tester, "architect": architect,
        "learner": cleaner,
    }
    stubbed.setattr(rt.registry, "build",
                    lambda role, repo_path=None: agents[role])

    rt.run(repo="PosClientBackend", title="t", body="b",
           apply=True, open_mr=False, ticket_id="T1")

    # Rollback was invoked exactly once with the pre-step head.
    assert len(reset_calls) == 1
    assert reset_calls[0][1] == head_before


def test_approved_step_does_not_rollback(stubbed, tmp_repo):
    """Validator approves → no reset call."""
    head_before = rt._git_head_sha(tmp_repo, branch="main")

    reset_calls: list[tuple[str, str]] = []
    stubbed.setattr(rt, "_git_head_sha",
                    lambda repo_path, branch: head_before)
    stubbed.setattr(rt, "_git_reset_to",
                    lambda *a: reset_calls.append(a) or True)
    stubbed.setattr(rt, "_resolve_repo_path", lambda r: tmp_repo)

    agents = {
        "understander": _FakeAgent([{"context_md": "ctx"}]),
        "planner": _FakeAgent([_plan()]),
        "grounder": _FakeAgent([{"resolved": True, "unresolved_refs": []}]),
        "verifier": _FakeAgent([{"verdict": "pass", "issues": []}]),
        "doer": _FakeAgent([{"applied": True, "udiff": "d",
                             "applied_branch": "aiforge/T1",
                             "step_id": 1, "target": "src/F0.java",
                             "problems": []}]),
        "validator": _FakeAgent([{"decision": "approve", "reason": "ok"}]),
        "tester": _FakeAgent([{"tests": [], "coverage_target": 0.8}]),
        "architect": _FakeAgent([{"decision": "approve",
                                  "comments": [], "mr_title": "",
                                  "mr_body": "", "mr_url": ""}]),
        "learner": _FakeAgent([{}]),
    }
    stubbed.setattr(rt.registry, "build",
                    lambda role, repo_path=None: agents[role])

    rt.run(repo="PosClientBackend", title="t", body="b",
           apply=True, open_mr=False, ticket_id="T1")

    assert reset_calls == []
