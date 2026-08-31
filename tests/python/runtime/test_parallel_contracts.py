"""Worker-declared interface contracts — the merger's language-agnostic blackboard.

Each worker ends its output with ``===CONTRACT=== {json}`` describing what its
file exposes and consumes. Those sidecars are what lets the merger reconcile a
Java or Go tree it cannot parse. Two things therefore matter most here: a
malformed or prose-trailing declaration must degrade to "no contract" rather
than raise, and concurrent workers in isolated worktrees must all land their
sidecars in ONE shared directory — the project root, not their own worktree.
"""
from __future__ import annotations

import json
import os

import pytest

from aiforge_core.runtime.parallel_subtasks import _contracts as ct


# ─── path → module ─────────────────────────────────────────────────────


@pytest.mark.parametrize("path,mod", [
    ("app/store.py", "app.store"),
    ("/app/store.py", "app.store"),
    ("src/main/java/App.java", "src.main.java.App"),
    ("pkg/thing.go", "pkg.thing"),
    ("web/app.tsx", "web.app"),
    ("app/__init__.py", "app"),
    ("", ""),
    ("Makefile", "Makefile"),
])
def test_path_to_module(path, mod):
    assert ct._path_to_module(path) == mod


# ─── parsing a declaration ─────────────────────────────────────────────


def test_the_first_balanced_object_is_taken():
    assert ct._first_json_object('{"a": 1} trailing prose') == {"a": 1}


def test_nested_braces_do_not_end_the_object_early():
    assert ct._first_json_object('{"a": {"b": 2}} rest') == {"a": {"b": 2}}


def test_unbalanced_json_yields_nothing():
    assert ct._first_json_object('{"a": 1') is None


def test_malformed_json_yields_nothing():
    assert ct._first_json_object("{not json} more") is None


def test_no_object_at_all_yields_nothing():
    assert ct._first_json_object("just prose") is None


# ─── declared symbols ──────────────────────────────────────────────────


@pytest.mark.parametrize("decl,name", [
    ("class Board", "Board"),
    ("def drop(x)", "drop"),
    ("COLORS: dict", "COLORS"),
    ("public void run()", "run"),
    ("const MAX = 3", "MAX"),
    ("!!!", ""),
])
def test_the_declared_name_is_extracted(decl, name):
    assert ct._clean_symbol(decl) == name


# ─── writing a sidecar ─────────────────────────────────────────────────


_OUT = ('Built the store.\n\n===CONTRACT=== {"exposes": ["class Store", "def get(k)"],'
        ' "consumes": {"app.util": ["helper"]}}\nthanks!')


def test_a_declaration_lands_as_a_sidecar(tmp_path):
    ct._write_contract_sidecar(str(tmp_path), {"path": "app/store.py", "slug": "store"}, _OUT)
    rec = json.loads((tmp_path / ct._CONTRACT_DIR / "store.json").read_text())
    assert rec == {"module": "app.store", "path": "app/store.py",
                   "exposes": ["class Store", "def get(k)"],
                   "consumes": {"app.util": ["helper"]}}


def test_sidecars_from_isolated_worktrees_land_in_the_shared_root(tmp_path):
    """Every worker runs in its own worktree; the merger needs all the contracts
    in one directory, so the write climbs back out to the project root."""
    wt = tmp_path / ".aiforge-worktrees" / "store"
    wt.mkdir(parents=True)
    ct._write_contract_sidecar(str(wt), {"path": "app/store.py", "slug": "store"}, _OUT)
    assert (tmp_path / ct._CONTRACT_DIR / "store.json").exists()
    assert not (wt / ct._CONTRACT_DIR).exists()


def test_output_without_a_declaration_writes_nothing(tmp_path):
    ct._write_contract_sidecar(str(tmp_path), {"path": "a.py", "slug": "a"}, "no contract here")
    assert not (tmp_path / ct._CONTRACT_DIR).exists()


def test_a_malformed_declaration_writes_nothing(tmp_path):
    ct._write_contract_sidecar(str(tmp_path), {"path": "a.py", "slug": "a"},
                               "===CONTRACT=== {broken")
    assert not (tmp_path / ct._CONTRACT_DIR).exists()


def test_a_missing_slug_falls_back_to_the_module_name(tmp_path):
    ct._write_contract_sidecar(str(tmp_path), {"path": "app/store.py"}, _OUT)
    assert (tmp_path / ct._CONTRACT_DIR / "app-store.json").exists()


def test_a_subtask_with_neither_slug_nor_path(tmp_path):
    ct._write_contract_sidecar(str(tmp_path), {}, _OUT)
    assert (tmp_path / ct._CONTRACT_DIR / "sub.json").exists()


def test_missing_keys_in_the_declaration_become_empty(tmp_path):
    ct._write_contract_sidecar(str(tmp_path), {"path": "a.py", "slug": "a"},
                               '===CONTRACT=== {"other": 1}')
    rec = json.loads((tmp_path / ct._CONTRACT_DIR / "a.json").read_text())
    assert rec["exposes"] == [] and rec["consumes"] == {}


# ─── reading sidecars back ─────────────────────────────────────────────


def _sidecar(root, name, rec):
    d = root / ct._CONTRACT_DIR
    d.mkdir(exist_ok=True)
    (d / name).write_text(json.dumps(rec) if isinstance(rec, dict) else rec)


def test_every_readable_sidecar_is_returned(tmp_path):
    _sidecar(tmp_path, "a.json", {"module": "a"})
    _sidecar(tmp_path, "b.json", {"module": "b"})
    _sidecar(tmp_path, "notes.txt", "ignored")
    mods = {r["module"] for r in ct._read_contract_files(str(tmp_path / ct._CONTRACT_DIR))}
    assert mods == {"a", "b"}


def test_a_corrupt_sidecar_does_not_lose_the_others(tmp_path):
    _sidecar(tmp_path, "good.json", {"module": "a"})
    _sidecar(tmp_path, "bad.json", "{not json")
    recs = ct._read_contract_files(str(tmp_path / ct._CONTRACT_DIR))
    assert [r["module"] for r in recs] == ["a"]


def test_declared_exposes_are_cleaned_to_names():
    exposes = ct._declared_exposes([{"module": "app.store",
                                     "exposes": ["class Store", "def get(k)", "!!!"]}])
    assert exposes == {"app.store": {"Store", "get"}}


def test_a_record_without_a_module_is_ignored():
    assert ct._declared_exposes([{"exposes": ["class X"]}]) == {}


def test_declared_consumes_match_a_target_loosely():
    consumes = ct._declared_consumes(
        [{"module": "app.cli", "consumes": {"store": ["get"]}}], {"app.store"})
    assert consumes == [("app.cli", "app.store", "get")]


def test_an_exact_target_is_preferred():
    consumes = ct._declared_consumes(
        [{"module": "cli", "consumes": {"app.store": ["get"]}}],
        {"app.store", "other.store"})
    assert consumes == [("cli", "app.store", "get")]


def test_a_consumed_module_nobody_declared_is_dropped():
    assert ct._declared_consumes(
        [{"module": "cli", "consumes": {"requests": ["get"]}}], {"app.store"}) == []


def test_the_blackboard_needs_a_contract_dir(tmp_path):
    assert ct._blackboard_from_contracts(str(tmp_path)) is None


def test_an_empty_contract_dir_falls_back_to_the_ast(tmp_path):
    (tmp_path / ct._CONTRACT_DIR).mkdir()
    assert ct._blackboard_from_contracts(str(tmp_path)) is None


def test_the_blackboard_pairs_exposes_with_consumes(tmp_path):
    _sidecar(tmp_path, "store.json",
             {"module": "app.store", "exposes": ["class Store"]})
    _sidecar(tmp_path, "cli.json",
             {"module": "app.cli", "exposes": ["def main()"],
              "consumes": {"app.store": ["Store", "missing"]}})
    exposes, consumes = ct._blackboard_from_contracts(str(tmp_path))
    assert exposes["app.store"] == {"Store"}
    assert ("app.cli", "app.store", "missing") in consumes


# ─── test-subtask recognition ──────────────────────────────────────────


@pytest.mark.parametrize("sub", [
    {"path": "tests/test_board.py"},
    {"path": "src/test/java/BoardTest.java"},
    {"path": "board_test.go"},
    {"path": "web/board.test.ts"},
    {"path": "web/board.spec.js"},
    {"slug": "test-board", "path": "x"},
])
def test_test_subtasks_are_recognised(sub):
    assert ct._is_test_subtask(sub) is True


@pytest.mark.parametrize("sub", [
    {"path": "app/board.py"}, {"path": "src/main/java/Board.java"},
    {"slug": "board", "path": "board.go"}, {},
])
def test_impl_subtasks_are_not(sub):
    assert ct._is_test_subtask(sub) is False


def test_a_top_level_spec_dir_is_not_recognised():
    """Known gap: the ``/spec`` check needs a leading separator, so the Ruby /
    JS convention of a ROOT-level ``spec/`` directory reads as an impl subtask
    and loses its test-first ordering. ``pkg/spec/…`` is recognised."""
    assert ct._is_test_subtask({"path": "spec/board_spec.rb"}) is False
    assert ct._is_test_subtask({"path": "pkg/spec/board_spec.rb"}) is True


# ─── matching tests for an impl ────────────────────────────────────────


def test_the_matching_test_source_is_read(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_board.py").write_text("def test_drop(): pass\n")
    (tmp_path / "tests/test_other.py").write_text("def test_other(): pass\n")
    out = ct._matching_tests_for(str(tmp_path), "app/board.py")
    assert "=== tests/test_board.py ===" in out
    assert "test_other" not in out


def test_no_impl_path_reads_nothing(tmp_path):
    assert ct._matching_tests_for(str(tmp_path), "") == ""


def test_a_path_with_no_stem_reads_nothing(tmp_path):
    assert ct._matching_tests_for(str(tmp_path), ".py") == ""


def test_git_and_venv_dirs_are_not_searched_for_tests(tmp_path):
    for d in (".git", ".venv", "__pycache__", ".aiforge-worktrees"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "test_board.py").write_text("x")
    assert ct._matching_tests_for(str(tmp_path), "board.py") == ""


def test_node_modules_is_still_searched(tmp_path):
    """Known gap: the skip list covers git/venv/pycache/worktrees but not
    node_modules, so a dependency's own test file matching the stem can end up
    in the impl worker's prompt (bounded only by the 8k cap)."""
    vend = tmp_path / "node_modules" / "dep"
    vend.mkdir(parents=True)
    (vend / "test_board.js").write_text("vendor test")
    assert "vendor test" in ct._matching_tests_for(str(tmp_path), "board.js")


def test_the_test_bundle_is_capped(tmp_path):
    (tmp_path / "tests").mkdir()
    for i in range(10):
        (tmp_path / f"tests/test_board_{i}.py").write_text("x" * 5000)
    assert len(ct._matching_tests_for(str(tmp_path), "board.py")) <= 8000


def test_an_unreadable_test_file_is_skipped(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_board.py").write_text("ok")
    real_open = open

    def _fussy(path, *a, **kw):
        if "test_board" in str(path):
            raise PermissionError("nope")
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", _fussy)
    assert ct._matching_tests_for(str(tmp_path), "board.py") == ""


# ─── merging the two phases' aggregates ────────────────────────────────


def test_aggregates_are_summed_and_ok_is_an_and():
    merged = ct._merge_aggs(
        {"ok": True, "total": 2, "done": 2, "failed": 0, "validated": 2,
         "merged": 2, "conflicts": ["a"], "review": "tests done"},
        {"ok": False, "total": 3, "done": 2, "failed": 1, "validated": 1,
         "merged": 2, "conflicts": ["b"], "review": "impl done"})
    assert merged == {"ok": False, "total": 5, "done": 4, "failed": 1,
                      "validated": 3, "merged": 4, "conflicts": ["a", "b"],
                      "review": "impl done"}


def test_the_impl_phase_review_wins_when_both_have_one():
    assert ct._merge_aggs({"review": "a"}, {"review": "b"})["review"] == "b"


def test_the_test_phase_review_is_used_when_the_impl_has_none():
    assert ct._merge_aggs({"review": "a"}, {})["review"] == "a"


def test_no_review_at_all_reads_as_done():
    assert ct._merge_aggs({}, {})["review"] == "done"


def test_empty_aggregates_merge_to_a_green_zero():
    assert ct._merge_aggs(None, None) == {
        "ok": True, "total": 0, "done": 0, "failed": 0, "validated": 0,
        "merged": 0, "conflicts": [], "review": "done"}
