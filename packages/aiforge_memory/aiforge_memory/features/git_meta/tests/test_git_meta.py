"""L15 — git_meta.read against a real ephemeral git repo."""
from __future__ import annotations

import subprocess

import pytest

from aiforge_memory.features.git_meta import extract as git_meta


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    )


@pytest.fixture()
def repo(tmp_path):
    """Ephemeral git repo with one commit + one origin remote."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("hello")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-m", "init")
    _git(tmp_path, "remote", "add", "origin",
         "https://github.com/example/repo.git")
    return tmp_path


def test_read_returns_full_metadata(repo):
    m = git_meta.read(repo)
    assert len(m.head_sha) == 40        # full sha
    assert m.branch == "main"
    assert m.remote_url == "https://github.com/example/repo.git"
    assert m.dirty is False


def test_dirty_true_when_tracked_file_modified(repo):
    (repo / "f.txt").write_text("changed")
    m = git_meta.read(repo)
    assert m.dirty is True


def test_dirty_false_with_only_untracked(repo):
    (repo / "untracked.txt").write_text("x")
    m = git_meta.read(repo)
    assert m.dirty is False             # -uno excludes untracked


def test_read_returns_empty_outside_git(tmp_path):
    m = git_meta.read(tmp_path)
    assert m.head_sha == ""
    assert m.branch == ""
    assert m.remote_url == ""
    assert m.dirty is False
