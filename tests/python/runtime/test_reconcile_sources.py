"""Source gathering, context selection, baseline snapshot, off-plan pruning.

The pruner DELETES files, so most of what is pinned here is the set of guards
that stop it: an existing repo, a pre-existing file, package glue, a plan with
no declared paths. The context selectors matter for a different reason — they
decide what the resolver gets to see, and "fell back to the whole tree" is the
failure mode that blows the window on a big repo.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from aiforge_core.runtime.parallel_subtasks._reconcile import _sources as src


def _write(root, rel, body="x = 1\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


# ─── _gather_sources ───────────────────────────────────────────────────


def test_sources_are_gathered_across_languages(tmp_path):
    for rel in ("app/store.py", "src/Main.java", "pkg/main.go", "web/app.ts",
                "conf/settings.toml"):
        _write(tmp_path, rel)
    found = {rel for rel, _c in src._gather_sources(str(tmp_path))}
    assert {"app/store.py", "src/Main.java", "pkg/main.go", "web/app.ts"} <= found


def test_content_comes_back_with_the_path(tmp_path):
    _write(tmp_path, "a.py", "print('hi')\n")
    assert ("a.py", "print('hi')\n") in src._gather_sources(str(tmp_path))


@pytest.mark.parametrize("junk", [
    ".git/config.py", "node_modules/dep/index.js", "__pycache__/a.py",
    "target/App.java", "build/x.py", "dist/x.js", ".venv/lib/x.py",
    ".aiforge-worktrees/w/a.py", ".pytest_cache/x.py",
])
def test_dependency_and_artifact_trees_are_skipped(tmp_path, junk):
    _write(tmp_path, junk)
    _write(tmp_path, "real.py")
    assert [rel for rel, _c in src._gather_sources(str(tmp_path))] == ["real.py"]


def test_non_source_files_are_ignored(tmp_path):
    _write(tmp_path, "README.md")
    _write(tmp_path, "logo.png")
    assert src._gather_sources(str(tmp_path)) == []


def test_an_unreadable_file_does_not_abort_the_walk(tmp_path, monkeypatch):
    _write(tmp_path, "ok.py")
    _write(tmp_path, "bad.py")
    real_open = open

    def _fussy(path, *a, **kw):
        if str(path).endswith("bad.py"):
            raise PermissionError("nope")
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", _fussy)
    assert [rel for rel, _c in src._gather_sources(str(tmp_path))] == ["ok.py"]


# ─── _spec_goal ────────────────────────────────────────────────────────


def test_the_goal_section_is_extracted(tmp_path):
    (tmp_path / "SPEC.md").write_text(
        "# SPEC\n\n## Goal\nBuild an LRU cache.\n\n## Subtasks\n- a\n")
    assert src._spec_goal(str(tmp_path)) == "Build an LRU cache."


def test_a_goal_at_the_end_of_the_file_is_extracted(tmp_path):
    (tmp_path / "SPEC.md").write_text("# SPEC\n\n## Goal\nShip it.\n")
    assert src._spec_goal(str(tmp_path)) == "Ship it."


def test_no_spec_means_no_goal(tmp_path):
    assert src._spec_goal(str(tmp_path)) == ""


def test_a_spec_without_a_goal_section(tmp_path):
    (tmp_path / "SPEC.md").write_text("# SPEC\n\n## Subtasks\n- a\n")
    assert src._spec_goal(str(tmp_path)) == ""


def test_a_very_long_goal_is_capped(tmp_path):
    (tmp_path / "SPEC.md").write_text("## Goal\n" + "word " * 1000)
    assert len(src._spec_goal(str(tmp_path))) <= 2000


# ─── _files_in_output ──────────────────────────────────────────────────


def test_files_named_in_the_error_are_found(tmp_path):
    _write(tmp_path, "app/store.py")
    _write(tmp_path, "app/cli.py")
    out = "E   ImportError in app/store.py:12\n  also app/cli.py"
    assert src._files_in_output(str(tmp_path), out) == {"app/store.py", "app/cli.py"}


def test_a_file_named_in_the_error_but_absent_is_not_returned(tmp_path):
    assert src._files_in_output(str(tmp_path), "boom in app/ghost.py") == set()


def test_absolute_paths_are_relativised(tmp_path):
    _write(tmp_path, "app/store.py")
    out = f"error at {tmp_path}/app/store.py:1"
    assert src._files_in_output(str(tmp_path), out) == {"app/store.py"}


def test_windows_separators_are_normalised(tmp_path):
    _write(tmp_path, "app/store.py")
    assert src._files_in_output(str(tmp_path), r"at app\store.py:3") == {"app/store.py"}


# ─── import graph ──────────────────────────────────────────────────────


def test_imported_basenames_covers_both_import_forms():
    import ast
    tree = ast.parse("import a.b.c\nfrom d.e import f\nimport plain\n")
    assert src._imported_basenames(tree) == {"c", "e", "plain"}


def test_local_module_files_finds_the_module(tmp_path):
    _write(tmp_path, "pkg/store.py")
    _write(tmp_path, "other/store.py")
    assert src._local_module_files(str(tmp_path), "store") == {
        "pkg/store.py", "other/store.py"}


def test_local_module_files_skips_the_aiforge_sidecars(tmp_path):
    _write(tmp_path, ".aiforge-contracts/store.py")
    assert src._local_module_files(str(tmp_path), "store") == set()


def test_python_local_imports_are_resolved(tmp_path):
    _write(tmp_path, "app/store.py")
    _write(tmp_path, "app/cli.py", "from app.store import get\n")
    assert src._py_local_imports(str(tmp_path), "app/cli.py") == {"app/store.py"}


def test_a_syntax_error_yields_no_imports(tmp_path):
    _write(tmp_path, "bad.py", "def (:\n")
    assert src._py_local_imports(str(tmp_path), "bad.py") == set()


def test_a_missing_file_yields_no_imports(tmp_path):
    assert src._py_local_imports(str(tmp_path), "gone.py") == set()


# ─── _relevant_files ───────────────────────────────────────────────────


def test_relevant_files_walks_the_failing_chain(tmp_path):
    _write(tmp_path, "app/store.py", "VALUE = 1\n")
    _write(tmp_path, "app/cli.py", "from app.store import VALUE\n")
    _write(tmp_path, "app/unrelated.py")
    picked = {rel for rel, _c in src._relevant_files(str(tmp_path), "fail in app/cli.py")}
    assert picked == {"app/cli.py", "app/store.py"}


def test_an_output_naming_nothing_falls_back_to_the_whole_tree(tmp_path):
    _write(tmp_path, "a.py")
    _write(tmp_path, "b.py")
    picked = {rel for rel, _c in src._relevant_files(str(tmp_path), "cold start timeout")}
    assert picked == {"a.py", "b.py"}


def test_the_chain_walk_is_capped(tmp_path, monkeypatch):
    """A 15-file cap is what keeps a big repo's import graph out of the window."""
    _write(tmp_path, "seed.py", "import m0\n")
    for i in range(30):
        _write(tmp_path, f"m{i}.py", f"import m{i + 1}\n")
    picked = src._relevant_files(str(tmp_path), "fail in seed.py")
    assert len(picked) <= 32          # bounded, not the unbounded transitive closure
    assert any(rel == "seed.py" for rel, _c in picked)


# ─── baseline snapshot ─────────────────────────────────────────────────


def test_the_baseline_records_the_pre_existing_files(tmp_path):
    _write(tmp_path, "app/store.py")
    _write(tmp_path, "app/cli.py")
    assert src._snapshot_baseline(str(tmp_path)) == 2
    assert src._baseline_set(str(tmp_path)) == {"app/store.py", "app/cli.py"}


def test_an_unwritable_baseline_still_reports_the_count(tmp_path, monkeypatch):
    _write(tmp_path, "a.py")
    real_open = open

    def _fussy(path, *a, **kw):
        if str(path).endswith(src._BASELINE_FILE):
            raise PermissionError("read-only")
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", _fussy)
    assert src._snapshot_baseline(str(tmp_path)) == 1


def test_no_baseline_file_is_an_empty_set(tmp_path):
    assert src._baseline_set(str(tmp_path)) == set()


# ─── off-plan pruning ──────────────────────────────────────────────────


@pytest.fixture()
def greenfield(monkeypatch):
    """The pruner's guard resolves to the LAST _is_greenfield defined in the
    module (the git-baseline one shadows the file-count one). Pin it directly."""
    monkeypatch.setattr(src, "_is_greenfield", lambda cwd: True)


def test_a_plan_with_no_declared_paths_prunes_nothing(tmp_path):
    _write(tmp_path, "phantom.py")
    assert src._prune_offplan_files(str(tmp_path), [{"goal": "no path"}]) == []
    assert (tmp_path / "phantom.py").exists()


def test_an_existing_repo_is_never_pruned(tmp_path, monkeypatch):
    """The guard that stops the pruner deleting a real codebase."""
    monkeypatch.setattr(src, "_is_greenfield", lambda cwd: False)
    _write(tmp_path, "phantom.py")
    assert src._prune_offplan_files(str(tmp_path), [{"path": "app/store.py"}]) == []
    assert (tmp_path / "phantom.py").exists()


def test_off_plan_files_are_removed(tmp_path, greenfield):
    _write(tmp_path, "app/store.py")
    _write(tmp_path, "tetris/game.py")          # the phantom duplicate
    removed = src._prune_offplan_files(str(tmp_path), [{"path": "app/store.py"}])
    assert removed == ["tetris/game.py"]
    assert not (tmp_path / "tetris/game.py").exists()
    assert (tmp_path / "app/store.py").exists()


def test_pre_existing_files_survive_the_prune(tmp_path, greenfield):
    _write(tmp_path, "legacy.py")
    src._snapshot_baseline(str(tmp_path))       # legacy.py is now baseline
    _write(tmp_path, "phantom.py")
    removed = src._prune_offplan_files(str(tmp_path), [{"path": "app/store.py"}])
    assert removed == ["phantom.py"]
    assert (tmp_path / "legacy.py").exists()


@pytest.mark.parametrize("glue", ["__init__.py", "pkg/conftest.py"])
def test_package_glue_is_kept(tmp_path, greenfield, glue):
    _write(tmp_path, glue)
    assert src._prune_offplan_files(str(tmp_path), [{"path": "app/store.py"}]) == []
    assert (tmp_path / glue).exists()


def test_build_and_config_files_are_kept(tmp_path, greenfield):
    """Non-source is out of scope — deleting a pyproject would break the build."""
    _write(tmp_path, "pyproject.toml")
    _write(tmp_path, "data.json")
    assert src._prune_offplan_files(str(tmp_path), [{"path": "app/store.py"}]) == []
    assert (tmp_path / "pyproject.toml").exists()


def test_a_failed_delete_is_not_reported_as_removed(tmp_path, greenfield, monkeypatch):
    _write(tmp_path, "phantom.py")
    monkeypatch.setattr(src.os, "remove", lambda p: (_ for _ in ()).throw(OSError("busy")))
    assert src._prune_offplan_files(str(tmp_path), [{"path": "app/store.py"}]) == []


def test_declared_paths_are_normalised():
    """A leading slash is stripped and ``..`` removed — note the order: the
    strip runs first, so ``../etc/x.py`` normalises to ``/etc/x.py`` and stays
    absolute. Harmless here (every path it is compared against comes from
    ``os.path.relpath``, so an absolute entry simply never matches), but it
    means this set is a comparison key, not a safe path to open."""
    assert src._spec_declared_paths([
        {"path": "/app/store.py"}, {"path": "../etc/passwd.py"}, {"goal": "no path"},
    ]) == {"app/store.py", "/etc/passwd.py"}


# ─── git-derived turn state ────────────────────────────────────────────


class _R:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _git_stub(monkeypatch, handler):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: handler(cmd))


def test_working_tree_changes_are_parsed(monkeypatch):
    _git_stub(monkeypatch, lambda cmd: _R(" M app/store.py\n?? new.py\n"))
    assert src._working_tree_changes("/cwd") == {"app/store.py", "new.py"}


def test_quoted_paths_are_unquoted(monkeypatch):
    _git_stub(monkeypatch, lambda cmd: _R(' M "app/with space.py"\n'))
    assert src._working_tree_changes("/cwd") == {"app/with space.py"}


def test_a_git_error_reads_as_unknown_not_empty(monkeypatch):
    """None and set() mean different things: None keeps the repair loop on."""
    _git_stub(monkeypatch, lambda cmd: _R("", returncode=128))
    assert src._working_tree_changes("/cwd") is None


def test_a_missing_git_reads_as_unknown(monkeypatch):
    def _boom(cmd, **kw):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", _boom)
    assert src._working_tree_changes("/cwd") is None


def test_files_committed_since_the_baseline_are_found(monkeypatch):
    def _h(cmd):
        if "rev-list" in cmd:
            return _R("abc123\n")
        return _R("app/store.py\napp/cli.py\n")
    _git_stub(monkeypatch, _h)
    assert src._committed_since_baseline("/cwd") == {"app/store.py", "app/cli.py"}


def test_no_baseline_commit_means_no_committed_files(monkeypatch):
    _git_stub(monkeypatch, lambda cmd: _R(""))
    assert src._committed_since_baseline("/cwd") == set()


def test_the_turn_sees_both_committed_and_uncommitted_source(monkeypatch):
    """Subtasks COMMIT their work — a working-tree-only view would call a whole
    greenfield build "nothing changed"."""
    def _h(cmd):
        if "status" in cmd:
            return _R(" M loose.py\n")
        if "rev-list" in cmd:
            return _R("abc\n")
        return _R("committed.py\nREADME.md\n")
    _git_stub(monkeypatch, _h)
    assert sorted(src._turn_changed_source("/cwd")) == ["committed.py", "loose.py"]


def test_turn_changed_source_is_none_on_a_broken_git(monkeypatch):
    _git_stub(monkeypatch, lambda cmd: _R("", returncode=1))
    assert src._turn_changed_source("/cwd") is None


# ─── _is_greenfield (the git-baseline definition wins) ─────────────────


def test_greenfield_when_the_baseline_commit_had_no_source(monkeypatch):
    def _h(cmd):
        if "rev-list" in cmd:
            return _R("abc\n")
        return _R("README.md\n.gitignore\n")
    _git_stub(monkeypatch, _h)
    assert src._is_greenfield("/cwd") is True


def test_not_greenfield_when_the_baseline_had_source(monkeypatch):
    def _h(cmd):
        if "rev-list" in cmd:
            return _R("abc\n")
        return _R("app/store.py\n")
    _git_stub(monkeypatch, _h)
    assert src._is_greenfield("/cwd") is False


def test_no_baseline_marker_is_not_greenfield(monkeypatch):
    """Can't tell → the conservative answer, since the greenfield-only steps are
    destructive on a real repo."""
    _git_stub(monkeypatch, lambda cmd: _R(""))
    assert src._is_greenfield("/cwd") is False


def test_an_ls_tree_failure_is_not_greenfield(monkeypatch):
    def _h(cmd):
        if "rev-list" in cmd:
            return _R("abc\n")
        return _R("", returncode=128)
    _git_stub(monkeypatch, _h)
    assert src._is_greenfield("/cwd") is False


def test_a_git_exception_is_not_greenfield(monkeypatch):
    def _boom(cmd, **kw):
        raise OSError("no git")
    monkeypatch.setattr(subprocess, "run", _boom)
    assert src._is_greenfield("/cwd") is False


# ─── _change_in_error ──────────────────────────────────────────────────


def test_a_changed_file_named_in_the_error_is_repairable(monkeypatch):
    monkeypatch.setattr(src, "_turn_changed_source", lambda cwd: ["app/store.py"])
    assert src._change_in_error("/cwd", "E ImportError: app/store.py") is True


def test_a_basename_match_counts(monkeypatch):
    monkeypatch.setattr(src, "_turn_changed_source", lambda cwd: ["app/store.py"])
    assert src._change_in_error("/cwd", "cannot import name from store") is True


def test_an_error_naming_none_of_the_changes_is_pre_existing(monkeypatch):
    monkeypatch.setattr(src, "_turn_changed_source", lambda cwd: ["app/store.py"])
    assert src._change_in_error("/cwd", "connection refused to localhost:5432") is False


def test_nothing_changed_means_not_our_failure(monkeypatch):
    monkeypatch.setattr(src, "_turn_changed_source", lambda cwd: [])
    assert src._change_in_error("/cwd", "anything") is False


def test_an_unusable_git_keeps_the_repair_loop_on(monkeypatch):
    """True on doubt: wrongly skipping a real regression is the worse error."""
    monkeypatch.setattr(src, "_turn_changed_source", lambda cwd: None)
    assert src._change_in_error("/cwd", "") is True
