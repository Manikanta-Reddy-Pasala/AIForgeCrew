from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aiforge_core.git_ops import GitError, GitOps
from aiforge_core.permissions import PermissionDenied

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Fresh git repo mirroring AIForgeCrew's security config (so ACLs apply)."""
    r = tmp_path / "repo"
    r.mkdir()
    # Copy config files needed by permissions.
    (r / "agents").symlink_to(REPO_ROOT / "agents", target_is_directory=True)
    (r / "security").symlink_to(REPO_ROOT / "security", target_is_directory=True)
    (r / "src").mkdir()
    (r / "tests").mkdir()
    (r / "src" / "a.py").write_text("x = 1\n")
    (r / "tests" / "a_test.py").write_text("assert True\n")

    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True)
    return r


def test_branch_allowed_for_tester(tmp_repo: Path) -> None:
    g = GitOps(tmp_repo)
    r = g.branch("tester", "feat/X")
    assert r["branch"] == "feat/X"


def test_branch_denied_for_sr_architect(tmp_repo: Path) -> None:
    g = GitOps(tmp_repo)
    with pytest.raises(PermissionDenied):
        g.branch("sr-architect", "feat/X")


def test_tester_commits_tests_only(tmp_repo: Path) -> None:
    g = GitOps(tmp_repo)
    g.branch("tester", "feat/Y")
    (tmp_repo / "tests" / "new_test.py").write_text("assert 1\n")
    r = g.commit("tester", ["tests/new_test.py"], "test: add case")
    assert r["commit"]


def test_tester_cannot_commit_src(tmp_repo: Path) -> None:
    g = GitOps(tmp_repo)
    g.branch("tester", "feat/Z")
    (tmp_repo / "src" / "leak.py").write_text("pass\n")
    with pytest.raises(PermissionDenied):
        g.commit("tester", ["src/leak.py"], "sneak")


def test_sr_developer_cannot_commit_tests(tmp_repo: Path) -> None:
    g = GitOps(tmp_repo)
    g.branch("sr-developer", "feat/W")
    (tmp_repo / "tests" / "dev_test.py").write_text("assert 1\n")
    with pytest.raises(PermissionDenied):
        g.commit("sr-developer", ["tests/dev_test.py"], "dev touches tests")


def test_create_mr_gh_missing_returns_intent(tmp_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    g = GitOps(tmp_repo)
    r = g.create_mr(
        "sr-architect", title="t", body="b",
        source_branch="feat/X", target_branch="main",
    )
    assert r["ok"] is False and r["reason"] == "gh_cli_missing"


def test_create_mr_denied_for_dev(tmp_repo: Path) -> None:
    g = GitOps(tmp_repo)
    with pytest.raises(PermissionDenied):
        g.create_mr("sr-developer", title="t", body="b", source_branch="feat/X")
