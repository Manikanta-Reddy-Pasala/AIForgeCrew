"""S1 — diff_preview must never read files OUTSIDE the repo root.

A path that resolves outside the repo (absolute outside path, ``../``
traversal, escaping symlink) is refused: the preview shows a redacted
placeholder instead of leaking the file's contents into the approval
preview / transcript.
"""
from __future__ import annotations

import os

from aiforge_core.runtime import diff_preview as dp

_SUPPRESSED = "[diff preview suppressed: path outside repo]"


def test_absolute_path_outside_repo_suppressed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET TOKEN\n")
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(repo))

    out = dp.unified_preview(str(secret), "new content", cwd=str(repo))
    assert out == _SUPPRESSED
    assert "SECRET" not in out


def test_dotdot_traversal_suppressed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET TOKEN\n")
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(repo))

    out = dp.unified_preview("../secret.txt", "x", cwd=str(repo))
    assert out == _SUPPRESSED
    assert "SECRET" not in out


def test_deep_traversal_to_etc_suppressed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(repo))
    out = dp.unified_preview("../../../../etc/passwd", "x", cwd=str(repo))
    assert out == _SUPPRESSED


def test_escaping_symlink_suppressed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n")
    link = repo / "link.txt"
    os.symlink(str(secret), str(link))
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(repo))

    out = dp.unified_preview("link.txt", "x", cwd=str(repo))
    assert out == _SUPPRESSED


def test_in_repo_file_real_diff(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    f = repo / "app.py"
    f.write_text("print('old')\n")
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(repo))

    out = dp.unified_preview("app.py", "print('new')\n", cwd=str(repo))
    assert out != _SUPPRESSED
    assert "-print('old')" in out
    assert "+print('new')" in out


def test_new_in_repo_file_diff_vs_empty(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(repo))

    out = dp.unified_preview("new_module.py", "x = 1\n", cwd=str(repo))
    assert out != _SUPPRESSED
    assert "+x = 1" in out
