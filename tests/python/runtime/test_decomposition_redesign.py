"""Regression tests for the decomposition redesign (analysis fan-out +
shared-worktree wave scheduler) and the post-review hardening fixes.

Pure-logic only — no ADK/agent execution — so they run without the model stack.
"""
import os
import tempfile

import pytest

from aiforge_core.runtime import analysis_pipeline as ap
from aiforge_core.runtime import parallel_subtasks as ps


# ─────────────────────────── schedule_waves (P2) ──────────────────────────
def _names(waves):
    return [[s["slug"] for s in w] for w in waves]


def test_waves_disjoint_files_group_together():
    subs = [{"slug": "a", "files": ["a.py"]},
            {"slug": "b", "files": ["b.py"]},
            {"slug": "c", "files": ["c.py"]}]
    assert _names(ps.schedule_waves(subs)) == [["a", "b", "c"]]


def test_waves_shared_file_serialized():
    subs = [{"slug": "a", "files": ["x.py"]},
            {"slug": "b", "files": ["x.py"]},
            {"slug": "c", "files": ["c.py"]}]
    waves = _names(ps.schedule_waves(subs))
    assert waves == [["a", "c"], ["b"]]


def test_waves_respect_deps():
    subs = [{"slug": "a", "files": ["a.py"]},
            {"slug": "b", "files": ["b.py"], "deps": ["a"]},
            {"slug": "c", "files": ["c.py"], "deps": ["a"]}]
    assert _names(ps.schedule_waves(subs)) == [["a"], ["b", "c"]]


def test_waves_dep_cycle_does_not_hang():
    subs = [{"slug": "x", "files": ["x.py"], "deps": ["y"]},
            {"slug": "y", "files": ["y.py"], "deps": ["x"]}]
    waves = _names(ps.schedule_waves(subs))
    assert sorted(s for w in waves for s in w) == ["x", "y"]


# ─────────────────────────── _files_of (goal recovery) ────────────────────
def test_files_of_explicit_fields_win():
    assert ps._files_of({"slug": "a", "files": ["f.py"]}) == {"f.py"}
    assert ps._files_of({"slug": "a", "path": "p.py"}) == {"p.py"}


def test_files_of_recovers_file_from_goal_even_with_verb_prefix():
    # .search (not .match) — a leading verb must not hide the file.
    assert ps._files_of({"slug": "a", "goal": "config.yaml: add key"}) == {"config.yaml"}
    assert ps._files_of({"slug": "a", "goal": "update config.yaml: add key"}) == {"config.yaml"}


def test_files_of_no_file_in_goal_is_empty():
    assert ps._files_of({"slug": "a", "goal": "Refactor: split the module"}) == set()
    assert ps._files_of({"slug": "a", "goal": "wire up auth"}) == set()


# ─────────────────────────── shared-worktree default OFF ───────────────────
def test_shared_worktree_defaults_off(monkeypatch):
    monkeypatch.delenv("AIFORGE_SHARED_WORKTREE", raising=False)
    assert ps._shared_worktree_enabled() is False
    monkeypatch.setenv("AIFORGE_SHARED_WORKTREE", "1")
    assert ps._shared_worktree_enabled() is True


# ─────────────────────────── identify_repos (analysis) ─────────────────────
def _mk_repos(*names):
    parent = tempfile.mkdtemp()
    for n in names:
        os.makedirs(os.path.join(parent, n, ".git"))
    return parent


def test_named_repo_path_does_not_pull_siblings():
    parent = _mk_repos("AIForgeCrew", "oneshell", "random")
    one = os.path.join(parent, "AIForgeCrew")
    repos = ap.identify_repos(f"summarize the architecture of {one}", parent)
    assert [r["name"] for r in repos] == ["AIForgeCrew"]


def test_unnamed_parent_scans_all_children():
    parent = _mk_repos("alpha", "beta", "gamma")
    repos = ap.identify_repos("read all the repos and summarize", parent)
    assert sorted(r["name"] for r in repos) == ["alpha", "beta", "gamma"]


def test_non_git_path_ignored():
    parent = _mk_repos("AIForgeCrew")
    one = os.path.join(parent, "AIForgeCrew")
    repos = ap.identify_repos("check /etc/hosts and the config", one)
    assert [r["name"] for r in repos] == ["AIForgeCrew"]


def test_repo_cap(monkeypatch):
    monkeypatch.setenv("AIFORGE_ANALYSIS_MAX_REPOS", "2")
    parent = _mk_repos("a1", "a2", "a3", "a4")
    repos = ap.identify_repos("read all repos", parent)
    assert len(repos) == 2


# ─────────────────────────── should_fan_out / topics ───────────────────────
def test_should_fan_out_two_repos():
    parent = _mk_repos("alpha", "beta")
    fan, repos, _ = ap.should_fan_out("read all repos and explore auth", parent)
    assert fan is True and len(repos) == 2


def test_should_not_fan_out_single_repo():
    parent = _mk_repos("solo")
    one = os.path.join(parent, "solo")
    fan, repos, _ = ap.should_fan_out("explain how auth works", one)
    assert fan is False and len(repos) == 1


def test_extract_topics_comma_and_list():
    assert ap.extract_topics("explore auth, sync, and data-model") == [
        "auth", "sync", "data-model"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
