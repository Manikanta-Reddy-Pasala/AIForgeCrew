"""Cover Architect's gh push/PR retry chain.

We don't shell out to real `git`/`gh` — instead we monkey-patch
`subprocess.run` and trace command shape + retry behaviour.
"""
from __future__ import annotations

import subprocess
from collections import deque
from pathlib import Path
from unittest import mock

import pytest

from aiforge_core.aiforge_agents.archetypes import architect as arch


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Don't actually back off in tests."""
    monkeypatch.setattr(arch, "_TRANSIENT_GH_PHRASES",
                        arch._TRANSIENT_GH_PHRASES)  # touch import
    monkeypatch.setenv("AIFORGE_GH_RETRY_MAX", "3")
    monkeypatch.setenv("AIFORGE_GH_RETRY_BASE_S", "0")
    monkeypatch.setenv("AIFORGE_GH_RETRY_CAP_S", "0")


def _cp(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc,
                                       stdout=stdout, stderr=stderr)


def _scripted_run(script: list[subprocess.CompletedProcess]):
    q = deque(script)

    def _run(args, **kw):  # noqa: ARG001
        if not q:
            raise AssertionError(f"unscripted call: {args}")
        cp = q.popleft()
        cp.args = args
        return cp

    return _run, q


def test_transient_classifier():
    assert arch._is_transient_gh_err("error: 503 Service Unavailable")
    assert arch._is_transient_gh_err("Connection reset by peer")
    assert arch._is_transient_gh_err("API rate limit exceeded")
    assert not arch._is_transient_gh_err("Authentication required")
    assert not arch._is_transient_gh_err("")


def test_push_retries_then_succeeds(monkeypatch, tmp_path):
    script = [
        _cp(0, "refs/remotes/origin/main\n"),     # symbolic-ref
        _cp(1, stderr="connection reset"),         # push #1
        _cp(1, stderr="503 service unavailable"),  # push #2
        _cp(0),                                    # push #3 OK
        _cp(0, "https://github.com/x/y/pull/1\n"),  # gh pr create OK
    ]
    runner, q = _scripted_run(script)
    monkeypatch.setattr(subprocess, "run", runner)

    url = arch._open_github_pr(
        repo_path=str(tmp_path), branch="aiforge/T1",
        title="t", body="b",
    )
    assert url == "https://github.com/x/y/pull/1"
    assert not q  # all scripted calls consumed


def test_push_aborts_on_permanent(monkeypatch, tmp_path):
    script = [
        _cp(0, "refs/remotes/origin/main\n"),  # symbolic-ref
        _cp(1, stderr="auth failed"),           # push #1 — permanent
    ]
    runner, q = _scripted_run(script)
    monkeypatch.setattr(subprocess, "run", runner)

    url = arch._open_github_pr(
        repo_path=str(tmp_path), branch="aiforge/T1",
        title="t", body="b",
    )
    assert url == ""
    assert not q


def test_pr_already_exists_falls_back_to_view(monkeypatch, tmp_path):
    script = [
        _cp(0, "refs/remotes/origin/main\n"),
        _cp(0),                                              # push OK
        _cp(1, stderr="a pull request for branch already exists"),
        _cp(0, "https://github.com/x/y/pull/42\n"),         # gh pr view
    ]
    runner, q = _scripted_run(script)
    monkeypatch.setattr(subprocess, "run", runner)

    url = arch._open_github_pr(
        repo_path=str(tmp_path), branch="aiforge/T1",
        title="t", body="b",
    )
    assert url == "https://github.com/x/y/pull/42"
    assert not q


def test_pr_create_retries_then_succeeds(monkeypatch, tmp_path):
    script = [
        _cp(0, "refs/remotes/origin/main\n"),
        _cp(0),                                  # push OK
        _cp(1, stderr="API rate limit exceeded"),  # pr create #1
        _cp(0, "https://github.com/x/y/pull/7\n"),  # pr create #2 OK
    ]
    runner, q = _scripted_run(script)
    monkeypatch.setattr(subprocess, "run", runner)

    url = arch._open_github_pr(
        repo_path=str(tmp_path), branch="aiforge/T1",
        title="t", body="b",
    )
    assert url == "https://github.com/x/y/pull/7"
    assert not q


def test_returns_empty_when_repo_missing():
    url = arch._open_github_pr(
        repo_path="/nope/does/not/exist", branch="x",
        title="t", body="b",
    )
    assert url == ""
