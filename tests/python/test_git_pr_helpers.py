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


# ─── _checkout_branch idempotent re-runs ────────────────────────────────


def test_checkout_branch_handles_existing_branch(tmp_path: Path) -> None:
    """The runner's pipeline can crash mid-run; on retry _checkout_branch
    must NOT fail when the branch already exists from the prior run.
    Uses 'checkout -B' which creates-or-resets."""
    _git_init(tmp_path)
    # Pre-create the branch (simulate prior run that crashed).
    subprocess.run(
        ["git", "branch", "ticket-foo"], cwd=tmp_path, check=True,
        capture_output=True,
    )
    reason = gp._checkout_branch(str(tmp_path), "ticket-foo")
    assert reason == ""
    # Confirm we're now on the branch.
    rc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert rc.stdout.strip() == "ticket-foo"


def test_checkout_branch_creates_when_absent(tmp_path: Path) -> None:
    _git_init(tmp_path)
    reason = gp._checkout_branch(str(tmp_path), "ticket-bar")
    assert reason == ""
    rc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert rc.stdout.strip() == "ticket-bar"


def test_checkout_branch_resets_existing_to_head(tmp_path: Path) -> None:
    """If branch existed at an older commit, ``checkout -B`` resets it
    to current HEAD. Important when Doer's mid-pipeline-crash branch
    is N commits behind main on retry."""
    _git_init(tmp_path)
    # Make a second commit on main.
    (tmp_path / "x.txt").write_text("x")
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "add", "x.txt"], cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "second"], cwd=tmp_path, check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
        text=True,
    ).stdout.strip()
    # Pre-create branch at the FIRST commit (older).
    subprocess.run(
        ["git", "branch", "stale", "HEAD~1"], cwd=tmp_path, check=True,
        capture_output=True,
    )
    reason = gp._checkout_branch(str(tmp_path), "stale")
    assert reason == ""
    new_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
        text=True,
    ).stdout.strip()
    assert new_head == head  # branch reset to current HEAD
