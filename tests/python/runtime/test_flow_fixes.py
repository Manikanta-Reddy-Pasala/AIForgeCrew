"""Regression tests for the flow-audit fixes:
- _is_managed_workspace / _commit_turn_baseline never commits a pinned user repo
- unified_query skips repo-agnostic sources for a scoped task (contamination)
- _ticket_looks_readonly detects read/comment-only tickets
"""
from __future__ import annotations

import subprocess

import pytest

from aiforge_core.runtime import parallel_subtasks as ps
from aiforge_core.runtime import adk_runner


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


# ── managed-workspace guard (regression #13) ──────────────────────────────────

def test_is_managed_workspace():
    assert ps._is_managed_workspace("/home/x/.aiforge/chat-workspaces/session-7")
    assert ps._is_managed_workspace("/repo/.aiforge-worktrees/CLR-9")
    assert not ps._is_managed_workspace("/home/x/my-project")
    assert not ps._is_managed_workspace("/home/x/chat-workspaces/notsession")


def test_baseline_does_not_commit_pinned_repo(tmp_path):
    # A user-pinned real repo with uncommitted WIP.
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "u@e")
    _git(tmp_path, "config", "user.name", "u")
    (tmp_path / "seed.py").write_text("x\n")
    _git(tmp_path, "add", "-A"); _git(tmp_path, "commit", "-m", "seed")
    (tmp_path / "wip.py").write_text("work in progress\n")   # user's WIP
    ps._commit_turn_baseline(str(tmp_path))
    # WIP must remain UNCOMMITTED (not swept into an agent commit).
    status = _git(tmp_path, "status", "--porcelain").stdout
    assert "wip.py" in status, "pinned-repo WIP must stay uncommitted"


def test_baseline_commits_managed_workspace(tmp_path, monkeypatch):
    # A managed session workspace path — leftover file folds into the baseline.
    ws = tmp_path / "chat-workspaces" / "session-3"
    ws.mkdir(parents=True)
    (ws / "leftover.py").write_text("stale\n")
    sha = ps._commit_turn_baseline(str(ws))
    assert sha
    assert _git(ws, "status", "--porcelain").stdout.strip() == ""


# ── ticket read-only detection (#6) ───────────────────────────────────────────

class _T:
    def __init__(self, title, body=""):
        self.title = title
        self.body = body


def test_ticket_readonly_detection():
    assert adk_runner._ticket_looks_readonly(_T("Analyze the GPS module and comment"))
    assert adk_runner._ticket_looks_readonly(_T("Review PR and assign reviewers"))
    assert not adk_runner._ticket_looks_readonly(_T("Add a GPS parser with tests"))
    assert not adk_runner._ticket_looks_readonly(_T("Fix the eviction bug"))
    # ask + change verb → treated as code (conservative)
    assert not adk_runner._ticket_looks_readonly(_T("Review and refactor the parser"))
