"""Fix 1 + Fix 2 — robust, unified scope matcher.

``scope_guard._matches_any`` must not false-block the LEGITIMATE paths the
planner commonly emits: directory globs (``src/``, ``src``), ``**`` globs
(``src/**/*.py``), single-segment globs (``src/*.py``), and an ABSOLUTE
path resolved under the repo root against a repo-relative glob. It must
STILL block a genuinely out-of-scope write, and an empty allowlist allows
all. ``parallel_subtasks._in_scope`` must agree (Fix 2: one shared matcher).
"""
from __future__ import annotations

from aiforge_core.runtime import parallel_subtasks as ps
from aiforge_core.runtime import scope_guard as sg


# ── Fix 1: directory globs + ** + single-segment all allow src/foo.py ──
def test_dir_glob_trailing_slash_allows_direct_child():
    assert sg._matches_any("src/foo.py", ["src/"]) is True


def test_dir_glob_no_slash_allows_direct_child():
    assert sg._matches_any("src/foo.py", ["src"]) is True


def test_double_star_glob_allows_direct_child():
    assert sg._matches_any("src/foo.py", ["src/**/*.py"]) is True


def test_double_star_glob_allows_nested_child():
    assert sg._matches_any("src/a/b.py", ["src/**/*.py"]) is True


def test_single_segment_glob_allows_direct_child():
    assert sg._matches_any("src/foo.py", ["src/*.py"]) is True


def test_single_segment_glob_does_not_span_dirs():
    # src/*.py is segment-scoped: it must NOT match a nested file.
    assert sg._matches_any("src/a/b.py", ["src/*.py"]) is False


# ── Fix 1: absolute path normalized to repo-relative ──
def test_absolute_path_under_repo_root_allowed(monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", "/abs/repo")
    monkeypatch.delenv("AIFORGE_WORKSPACE_DIR", raising=False)
    assert sg._matches_any(
        "/abs/repo/aiforge_core/db.py", ["aiforge_core/*.py"]) is True


def test_absolute_path_under_workspace_dir_allowed(monkeypatch):
    monkeypatch.setenv("AIFORGE_WORKSPACE_DIR", "/abs/ws")
    assert sg._matches_any(
        "/abs/ws/src/app.py", ["src/**"]) is True


# ── Fix 1: still blocks genuinely out-of-scope, and empty = allow ──
def test_out_of_scope_still_blocked():
    assert sg._matches_any("secrets.env", ["src/**"]) is False


def test_out_of_scope_absolute_still_blocked(monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", "/abs/repo")
    assert sg._matches_any(
        "/abs/repo/secrets.env", ["src/**"]) is False


def test_empty_globs_allow_all():
    assert sg._matches_any("anything/at/all.py", []) is True


# ── Fix 2: the two matchers agree on the directory-glob case ──
def test_in_scope_agrees_with_matches_any_on_dir_glob():
    for path, globs in (
        ("src/foo.py", ["src/"]),
        ("src/foo.py", ["src"]),
        ("src/a/b.py", ["src/**/*.py"]),
        ("secrets.env", ["src/**"]),
        ("src/a/b.py", ["src/*.py"]),
    ):
        assert ps._in_scope(path, globs) == sg._matches_any(path, globs)
