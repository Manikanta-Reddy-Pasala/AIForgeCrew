"""Unit tests for runtime.git_pr helpers — remote reachability probe
+ default .gitignore template. No actual ``git push``."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from aiforge_core.runtime import git_pr as gp


def _git_init(tmp: Path) -> Path:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-m", "init"],
                   cwd=tmp, check=True, capture_output=True)
    return tmp


# ─── _has_reachable_remote ────────────────────────────────────────────


def test_has_reachable_remote_no_origin(tmp_path: Path) -> None:
    _git_init(tmp_path)
    ok, reason = gp._has_reachable_remote(str(tmp_path))
    assert ok is False
    assert reason == "no_origin_configured"


def test_has_reachable_remote_unreachable_origin(tmp_path: Path) -> None:
    _git_init(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin",
         "https://github.com/no-such-user-foo/no-such-repo-bar.git"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    ok, reason = gp._has_reachable_remote(str(tmp_path))
    assert ok is False
    assert reason in ("remote_unreachable", "remote_unreachable_timeout",
                      "remote_probe_error: " + reason.split(":", 1)[-1].strip()
                      if reason.startswith("remote_probe_error") else "x")


# ─── _ensure_gitignore ───────────────────────────────────────────────


def test_ensure_gitignore_writes_when_absent(tmp_path: Path) -> None:
    gp._ensure_gitignore(str(tmp_path))
    gi = tmp_path / ".gitignore"
    assert gi.exists()
    body = gi.read_text(encoding="utf-8")
    # Spot-check the patterns that the Doer's stress run leaked.
    assert "__pycache__/" in body
    assert "*.db" in body
    assert ".venv/" in body
    assert ".DS_Store" in body


def test_ensure_gitignore_preserves_existing(tmp_path: Path) -> None:
    """Operator's existing .gitignore wins — runtime never overwrites."""
    gi = tmp_path / ".gitignore"
    gi.write_text("# operator-curated\nfoo/\n", encoding="utf-8")
    gp._ensure_gitignore(str(tmp_path))
    body = gi.read_text(encoding="utf-8")
    assert body == "# operator-curated\nfoo/\n"


def test_default_gitignore_template_covers_polyglot_artifacts() -> None:
    """Template must catch artifacts from Python/Node/Java scaffolds —
    the stress test surfaced .pyc + .db; future tickets may scaffold
    Maven (target/) or Node (node_modules/) projects."""
    body = gp._DEFAULT_GITIGNORE
    assert "__pycache__/" in body          # Python
    assert "node_modules/" in body          # Node
    assert "target/" in body                # Maven
    assert "*.db" in body                    # SQLite scratch
    assert "build/" in body                  # Gradle/Setuptools
