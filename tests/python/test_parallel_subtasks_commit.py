"""Parallel-subtask commit hygiene: the worktree commit + the workspace
baseline must NOT sweep the agent's own ``.aiforge-worktrees/`` artifacts
(or other junk) into git, and a fresh workspace is born with a
``.gitignore`` covering the artifacts."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aiforge_core.runtime import parallel_subtasks as ps


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True)


def _init_repo(repo: Path) -> None:
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["config", "commit.gpgsign", "false"], repo)
    (repo / "seed.txt").write_text("seed\n")
    _git(["add", "seed.txt"], repo)
    _git(["commit", "-q", "-m", "init"], repo)


def test_commit_all_excludes_artifacts(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git binary not on PATH")
    _init_repo(tmp_path)
    # Real work file + an agent artifact dir that must NOT be committed.
    (tmp_path / "real.py").write_text("x = 1\n")
    (tmp_path / ".aiforge-worktrees").mkdir()
    (tmp_path / ".aiforge-worktrees" / "junk.txt").write_text("junk\n")
    (tmp_path / "stray.pyc").write_text("bytecode\n")

    ps._commit_all(str(tmp_path), "my-slug")

    show = _git(["show", "--name-only", "--format=", "HEAD"], tmp_path)
    assert "real.py" in show.stdout
    assert ".aiforge-worktrees" not in show.stdout
    assert "junk.txt" not in show.stdout
    assert "stray.pyc" not in show.stdout


def test_ensure_git_workspace_writes_gitignore(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git binary not on PATH")
    ws = tmp_path / "ws"
    base = ps._ensure_git_workspace(str(ws))
    assert base  # a branch name
    gi = ws / ".gitignore"
    assert gi.exists()
    body = gi.read_text()
    for line in (".aiforge-worktrees/", ".aiforge-workspace",
                 "graphify-out/", "perf.ndjson"):
        assert line in body
    # The artifact marker is gitignored + excluded → not committed; the
    # .gitignore itself is the committed baseline so HEAD resolves.
    head = _git(["rev-parse", "HEAD"], ws)
    assert head.returncode == 0
    tracked = _git(["ls-files"], ws)
    assert ".gitignore" in tracked.stdout
    assert ".aiforge-workspace" not in tracked.stdout
