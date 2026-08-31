"""Scaffolding, file ownership, and decomposition consistency.

These three run BEFORE any worker does, and each exists to stop a specific
class of merge disaster: two agents authoring the same file, a plan whose
tests import modules nobody was told to write, and workers inventing their own
directory layouts because the tree was empty when they started.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.parallel_subtasks._reconcile import _scaffold as sc


# ─── stub content ──────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["pom.xml", "index.html", "data.json", "app.cfg",
                                  "x.properties", "notes.txt", "README.md", "Makefile"])
def test_build_and_markup_files_are_left_empty(path):
    """The owning worker writes these whole — a stub header would be garbage
    inside XML or JSON."""
    assert sc._stub_content(path, ["def x()"], False) == ""


def test_a_test_file_gets_a_one_line_marker():
    out = sc._stub_content("tests/test_board.py", [], True)
    assert out == f"# Tests — implement per SPEC.md. {sc._SCAFFOLD_MARK}\n"


@pytest.mark.parametrize("path,cmt", [("Main.java", "//"), ("main.go", "//"),
                                      ("app.ts", "//"), ("run.sh", "#"),
                                      ("lib.rs", "//"), ("conf.yaml", "#")])
def test_each_language_gets_its_own_comment_prefix(path, cmt):
    out = sc._stub_content(path, ["void run()"], False)
    assert out.startswith(f"{cmt} STUB {sc._SCAFFOLD_MARK}")
    assert f"{cmt}   void run()" in out


def test_an_unknown_extension_falls_back_to_a_hash():
    assert sc._stub_content("thing.zig", [], False).startswith("# STUB")


def test_a_file_with_no_declared_api_points_at_the_spec():
    assert "(see SPEC.md)" in sc._stub_content("Main.java", [], False)


# ─── python stubs ──────────────────────────────────────────────────────


def test_python_stubs_keep_signatures_so_sibling_imports_resolve():
    out = sc._stub_content("app/board.py", ["class Board", "def drop(col: int)"], False)
    assert out.startswith('"""Stub')
    assert "class Board:\n    ..." in out
    assert "def drop(col: int):\n    raise NotImplementedError" in out


def test_an_async_def_keeps_its_signature():
    assert "async def fetch(url):\n    raise NotImplementedError" in sc._python_stub(
        ["async def fetch(url)"])


def test_a_trailing_colon_in_the_contract_is_not_doubled():
    assert "def drop():\n" in sc._python_stub(["def drop():"])


def test_a_constant_becomes_a_module_level_name():
    assert "COLORS = None" in sc._python_stub(["COLORS: dict"])


def test_a_bare_name_becomes_a_module_level_name():
    assert "MAX = None" in sc._python_stub(["MAX"])


def test_an_unusable_contract_entry_is_dropped():
    assert sc._stub_line("!!!") is None
    assert sc._python_stub(["!!!"]).strip().endswith('"""')


def test_blank_contract_entries_are_ignored():
    assert sc._python_stub(["", "   ", None]).count("None") == 0


# ─── impl path for a test ──────────────────────────────────────────────


def test_java_mirrors_test_into_main():
    assert sc._impl_path_for_test(
        "src/test/java/app/BookServiceTest.java", "BookService", ".java", []
    ) == "src/main/java/app/BookService.java"


def test_java_without_a_test_dir_uses_an_existing_impl_dir():
    assert sc._impl_path_for_test("BookServiceTest.java", "BookService", ".java",
                                  ["src/main/java"]) == "src/main/java/BookService.java"


def test_java_with_nothing_to_go_on_lands_at_the_root():
    assert sc._impl_path_for_test("BookServiceTest.java", "BookService",
                                  ".java", []) == "BookService.java"


def test_an_impl_dir_wins_when_one_exists():
    assert sc._impl_path_for_test("tests/test_board.py", "board", ".py",
                                  ["app"]) == "app/board.py"


def test_the_tests_segment_is_stripped_from_the_parent():
    assert sc._impl_path_for_test("tests/test_board.py", "board", ".py",
                                  []) == "board.py"


def test_a_nested_tests_dir_keeps_the_package_but_drops_tests():
    assert sc._impl_path_for_test("pkg/tests/test_board.py", "board", ".py",
                                  []) == "pkg/board.py"


# ─── disjoint file ownership ───────────────────────────────────────────


def test_one_agent_per_file():
    """The plan is not trusted: two subtasks claiming a path would have two
    agents authoring the same file and clobbering each other on merge."""
    subs, folded = sc._enforce_disjoint_files([
        {"slug": "a", "path": "app/store.py", "goal": "write the store"},
        {"slug": "b", "path": "app/store.py", "goal": "add an index"},
    ])
    assert folded == 1
    assert len(subs) == 1
    assert subs[0]["goal"] == "write the store\n- also: add an index"


def test_paths_are_normalised_before_comparison():
    subs, folded = sc._enforce_disjoint_files([
        {"path": "app/store.py", "goal": "a"},
        {"path": "./app/store.py", "goal": "b"},
    ])
    assert folded == 1


def test_a_duplicate_goal_is_not_appended_twice():
    subs, _ = sc._enforce_disjoint_files([
        {"path": "app/store.py", "goal": "write the store"},
        {"path": "app/store.py", "goal": "write the store"},
    ])
    assert subs[0]["goal"] == "write the store"


def test_a_pathless_subtask_is_never_folded():
    subs, folded = sc._enforce_disjoint_files([{"slug": "a"}, {"slug": "b"}])
    assert folded == 0
    assert len(subs) == 2


def test_distinct_paths_are_untouched():
    plan = [{"path": "a.py", "goal": "a"}, {"path": "b.py", "goal": "b"}]
    subs, folded = sc._enforce_disjoint_files(plan)
    assert folded == 0
    assert subs == plan


# ─── test-stem → module ────────────────────────────────────────────────


@pytest.mark.parametrize("stem,target", [
    ("test_board", "board"),
    ("board_test", "board"),
    ("board_tests", "board"),
    ("BookServiceTest", "BookService"),
    ("BookServiceTests", "BookService"),
    ("PaymentIT", "Payment"),
    ("PaymentITCase", "Payment"),
    ("board.test", "board"),
    ("board.spec", "board"),
])
def test_the_targeted_module_is_recovered(stem, target):
    assert sc._test_target_module(stem) == target


@pytest.mark.parametrize("stem", ["board", "helpers", "conftest"])
def test_a_non_test_stem_targets_nothing(stem):
    assert sc._test_target_module(stem) is None


# ─── decomposition consistency ─────────────────────────────────────────


def test_a_test_without_its_module_gets_one_planned():
    """The architect collapsing all impl into one file while writing per-module
    tests is what produced un-importable tests and collection errors."""
    out = sc._ensure_impl_modules([
        {"slug": "app", "path": "app/main.py"},
        {"slug": "t", "path": "tests/test_board.py"},
    ])
    added = out[2:]
    assert [s["path"] for s in added] == ["app/board.py"]
    assert added[0]["slug"] == "board"
    assert "test_board.py" in added[0]["goal"]


def test_a_module_already_in_the_plan_is_not_added_twice():
    plan = [{"path": "app/board.py"}, {"path": "tests/test_board.py"}]
    assert sc._ensure_impl_modules(plan) == plan


def test_the_match_is_case_insensitive():
    plan = [{"path": "app/Board.py"}, {"path": "tests/test_board.py"}]
    assert sc._ensure_impl_modules(plan) == plan


@pytest.mark.parametrize("stem", ["integration", "e2e", "smoke", "system",
                                  "acceptance", "cli", "main", "app"])
def test_whole_system_tests_do_not_demand_a_module(stem):
    plan = [{"path": "app/main.py"}, {"path": f"tests/test_{stem}.py"}]
    assert sc._ensure_impl_modules(plan) == plan


def test_two_tests_for_the_same_missing_module_add_it_once():
    out = sc._ensure_impl_modules([
        {"path": "app/main.py"},
        {"path": "tests/test_board.py"},
        {"path": "tests/board_test.py"},
    ])
    assert [s["path"] for s in out[3:]] == ["app/board.py"]


def test_a_pathless_subtask_is_skipped():
    plan = [{"slug": "thinking"}, {"path": "app/main.py"}]
    assert sc._ensure_impl_modules(plan) == plan


def test_an_xml_directory_is_not_treated_as_an_impl_dir():
    """pom.xml's directory is not where Java sources go."""
    out = sc._ensure_impl_modules([
        {"path": "pom.xml"},
        {"path": "src/main/java/App.java"},
        {"path": "src/test/java/BookServiceTest.java"},
    ])
    assert out[3]["path"] == "src/main/java/BookService.java"


# ─── scaffolding the tree ──────────────────────────────────────────────


def test_every_declared_file_is_created(tmp_path):
    written = sc._scaffold_stubs(str(tmp_path), [
        {"path": "app/board.py", "api": ["class Board"]},
        {"path": "tests/test_board.py"},
    ])
    assert written == ["app/board.py", "tests/test_board.py"]
    assert "class Board:" in (tmp_path / "app/board.py").read_text()
    assert sc._SCAFFOLD_MARK in (tmp_path / "tests/test_board.py").read_text()


def test_an_existing_file_is_never_overwritten(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/board.py").write_text("REAL CODE\n")
    assert sc._scaffold_stubs(str(tmp_path), [{"path": "app/board.py"}]) == []
    assert (tmp_path / "app/board.py").read_text() == "REAL CODE\n"


def test_a_pathless_subtask_scaffolds_nothing(tmp_path):
    assert sc._scaffold_stubs(str(tmp_path), [{"slug": "thinking"}]) == []


@pytest.mark.parametrize("path", ["../../etc/evil.py", "../tmp/escaped.py"])
def test_a_path_that_lands_outside_the_workspace_is_refused(tmp_path, path):
    """Stripping ".." out of the string is not containment: "../tmp/x.py"
    becomes "/tmp/x.py", which os.path.join returns UNCHANGED because it is
    absolute — so a planned path (model output) could scaffold a file outside
    the workspace entirely. The check is where the path actually lands."""
    assert sc._scaffold_stubs(str(tmp_path), [{"path": path}]) == []
    assert list(tmp_path.iterdir()) == []


def test_a_leading_slash_is_relative_to_the_workspace(tmp_path):
    assert sc._scaffold_stubs(str(tmp_path), [{"path": "/app/board.py"}]) == ["app/board.py"]
    assert (tmp_path / "app/board.py").exists()


def test_an_inner_traversal_still_scaffolds(tmp_path):
    written = sc._scaffold_stubs(str(tmp_path), [{"path": "pkg/../app/board.py"}])
    assert written == ["pkg//app/board.py"]
    assert (tmp_path / "pkg/app/board.py").exists()


def test_a_file_at_the_root_scaffolds(tmp_path):
    assert sc._scaffold_stubs(str(tmp_path), [{"path": "main.py"}]) == ["main.py"]


def test_an_unwritable_destination_is_skipped_not_fatal(tmp_path, monkeypatch):
    def _boom(*_a, **_kw):
        raise PermissionError("read-only")
    monkeypatch.setattr(sc.os, "makedirs", _boom)
    assert sc._scaffold_stubs(str(tmp_path), [{"path": "app/board.py"}]) == []
