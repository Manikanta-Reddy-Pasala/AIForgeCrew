"""_commit_turn_baseline pins a clean per-turn baseline so a reused workspace's
leftover files (a previous ticket's edits) don't leak into THIS turn's Changes
diff / build-gate. Regression for: a no-code Jira turn wrongly showing a prior
ticket's file changes + running the integration pipeline on stale files."""
from __future__ import annotations

import subprocess

import pytest

from aiforge_core.runtime.parallel_subtasks import _commit_turn_baseline


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)


def test_baseline_folds_in_leftover_files(tmp_path):
    # Simulate a reused workspace that already holds a PREVIOUS task's files.
    (tmp_path / "old_ticket.py").write_text("def old(): return 1\n")
    sha = _commit_turn_baseline(str(tmp_path))
    assert sha, "should return a HEAD sha"
    # After the snapshot the tree is CLEAN — the leftover file is committed into
    # the baseline, not reported as this turn's work.
    status = _git(tmp_path, "status", "--porcelain").stdout
    assert status.strip() == "", f"tree should be clean, got: {status!r}"


def test_diff_against_baseline_shows_only_new_work(tmp_path):
    (tmp_path / "old_ticket.py").write_text("stale\n")
    sha = _commit_turn_baseline(str(tmp_path))
    # THIS turn writes one new file.
    (tmp_path / "new_work.py").write_text("def new(): return 2\n")
    # After the snapshot, the ONLY thing dirty is this turn's new file — the
    # leftover file is already committed into the baseline.
    status = _git(tmp_path, "status", "--porcelain").stdout
    dirty = [ln[3:].strip() for ln in status.splitlines() if ln.strip()]
    assert "new_work.py" in dirty
    assert "old_ticket.py" not in dirty, "prior ticket's file must NOT appear"


def test_second_snapshot_is_clean_after_first(tmp_path):
    (tmp_path / "a.py").write_text("1\n")
    _commit_turn_baseline(str(tmp_path))
    # A follow-up turn that writes nothing → snapshot is a clean no-op baseline.
    sha2 = _commit_turn_baseline(str(tmp_path))
    assert sha2
    assert _git(tmp_path, "status", "--porcelain").stdout.strip() == ""
