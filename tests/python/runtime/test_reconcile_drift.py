"""Dead-import pruning and the cross-module symbol-drift blackboard.

Two jobs with very different risk. The pruner DELETES import names, so it only
acts on an exact local-module match and only writes back source that still
parses. The drift report only SUGGESTS, so it matches modules loosely and
offers the closest real name.

The distinction the module exists to make: a name defined as a METHOD does not
satisfy ``from mod import name``. Module-level or it isn't exposed.
"""
from __future__ import annotations

import ast

import pytest

from aiforge_core.runtime.parallel_subtasks._reconcile import _drift as dr


def _write(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


# ─── path → module ─────────────────────────────────────────────────────


@pytest.mark.parametrize("rel,mod", [
    ("pkg/sub/__init__.py", "pkg.sub"),
    ("pkg/mod.py", "pkg.mod"),
    ("mod.py", "mod"),
    ("__init__.py", "__init__"),   # only a nested pkg/__init__ collapses
])
def test_rel_to_mod(rel, mod):
    assert dr._rel_to_mod(rel) == mod


# ─── reading the tree ──────────────────────────────────────────────────


def test_python_files_are_read(tmp_path):
    _write(tmp_path, "a.py", "x = 1\n")
    _write(tmp_path, "pkg/b.py", "y = 2\n")
    _write(tmp_path, "notes.md", "hi")
    assert set(dr._read_python_files(str(tmp_path))) == {"a.py", "pkg/b.py"}


@pytest.mark.parametrize("junk", [".git/x.py", "node_modules/m/x.py",
                                  "__pycache__/x.py", ".venv/x.py",
                                  ".aiforge-worktrees/w/x.py"])
def test_vendor_dirs_are_skipped(tmp_path, junk):
    _write(tmp_path, junk, "x = 1\n")
    assert dr._read_python_files(str(tmp_path)) == {}


def test_an_unreadable_file_is_skipped(tmp_path, monkeypatch):
    _write(tmp_path, "ok.py", "x = 1\n")
    _write(tmp_path, "bad.py", "y = 2\n")
    real_open = open

    def _fussy(path, *a, **kw):
        if str(path).endswith("bad.py"):
            raise PermissionError("nope")
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", _fussy)
    assert set(dr._read_python_files(str(tmp_path))) == {"ok.py"}


# ─── what a module exposes ─────────────────────────────────────────────


def test_module_level_symbols_include_reexported_imports():
    tree = ast.parse("import os\nfrom x import y as z\n"
                     "CONST = 1\ndef f(): pass\nclass C: pass\n")
    assert dr._module_level_symbols(tree) == {"os", "z", "CONST", "f", "C"}


def test_a_method_is_not_a_module_level_symbol():
    """The bug this file exists to catch: ``from mod import helper`` does not
    work because ``helper`` is a method on a class inside ``mod``."""
    tree = ast.parse("class C:\n    def helper(self): pass\n")
    assert dr._module_level_symbols(tree) == {"C"}


def test_defined_symbols_exclude_reexports():
    """A drift report should not credit a module for names it merely imported."""
    tree = ast.parse("import os\nfrom x import y\nCONST = 1\ndef f(): pass\n")
    assert dr._defined_symbols(tree) == {"CONST", "f"}


# ─── module resolution ─────────────────────────────────────────────────


def test_an_exact_module_name_resolves():
    assert dr._resolve_module("pkg.mod", {"pkg.mod": set()}) == "pkg.mod"


def test_a_suffix_match_resolves():
    assert dr._resolve_module("mod", {"pkg.mod": set()}) == "pkg.mod"


def test_a_third_party_module_resolves_to_nothing():
    assert dr._resolve_module("requests", {"pkg.mod": set()}) is None


def test_the_report_matcher_is_looser_than_the_pruner():
    """``a.mod`` against a local ``b.mod``: good enough to hint, not good enough
    to delete code over."""
    mods = {"b.mod"}
    assert dr._drift_target("a.mod", mods) == "b.mod"
    assert dr._resolve_module("a.mod", {"b.mod": set()}) is None


def test_drift_target_prefers_an_exact_match():
    assert dr._drift_target("pkg.mod", {"pkg.mod", "other.mod"}) == "pkg.mod"


def test_drift_target_gives_up_on_an_unknown_module():
    assert dr._drift_target("requests", {"pkg.mod"}) is None


# ─── dead names ────────────────────────────────────────────────────────


def test_a_name_the_local_module_lacks_is_dead():
    tree = ast.parse("from pkg.mod import present, missing\n")
    assert dr._dead_imported_names(tree, {"pkg.mod": {"present"}}) == {"missing"}


def test_third_party_imports_are_never_judged():
    tree = ast.parse("from requests import get\n")
    assert dr._dead_imported_names(tree, {"pkg.mod": set()}) == set()


def test_a_star_import_is_left_alone():
    tree = ast.parse("from pkg.mod import *\n")
    assert dr._dead_imported_names(tree, {"pkg.mod": {"a"}}) == set()


# ─── line surgery ──────────────────────────────────────────────────────


def test_only_the_dead_name_is_stripped():
    assert dr._strip_import_line("from pkg.mod import a, b", {"b"}) == \
        "from pkg.mod import a"


def test_an_aliased_dead_name_is_stripped():
    assert dr._strip_import_line("from pkg.mod import a as x, b", {"a"}) == \
        "from pkg.mod import b"


def test_a_line_with_nothing_left_is_dropped():
    assert dr._strip_import_line("from pkg.mod import a", {"a"}) is None


@pytest.mark.parametrize("line,dead,expected", [
    ('"gone",', {"gone"}, True),
    ("'gone',", {"gone"}, True),
    ('"kept",', {"gone"}, False),
    ("x = 1", {"gone"}, False),
])
def test_dead_all_entries(line, dead, expected):
    assert dr._is_dead_all_entry(line, dead) is expected


def test_dead_names_leave_both_the_import_and_the_all_entry():
    src = ('from pkg.mod import alive, gone\n\n'
           '__all__ = [\n    "alive",\n    "gone",\n]\n')
    out = dr._without_dead_names(src, {"gone"})
    assert "gone" not in out
    assert "alive" in out


# ─── writing back ──────────────────────────────────────────────────────


def test_a_rewrite_that_would_not_parse_is_refused(tmp_path):
    _write(tmp_path, "a.py", "x = 1\n")
    assert dr._rewrite_if_valid(str(tmp_path), "a.py", "def (:\n") is False
    assert (tmp_path / "a.py").read_text() == "x = 1\n"


def test_a_valid_rewrite_lands_with_a_trailing_newline(tmp_path):
    _write(tmp_path, "a.py", "x = 1\n")
    assert dr._rewrite_if_valid(str(tmp_path), "a.py", "y = 2") is True
    assert (tmp_path / "a.py").read_text() == "y = 2\n"


def test_parsed_returns_none_on_a_syntax_error():
    assert dr._parsed("def (:\n") is None
    assert dr._parsed("x = 1\n") is not None


# ─── the pruner end to end ─────────────────────────────────────────────


def test_a_reexport_of_a_method_is_pruned(tmp_path):
    """A package __init__ re-exporting a class METHOD fails every import of the
    package — the single most common cross-file break."""
    _write(tmp_path, "pkg/mod.py", "class C:\n    def helper(self): pass\n")
    _write(tmp_path, "pkg/__init__.py",
           'from pkg.mod import C, helper\n\n__all__ = ["C", "helper"]\n')
    assert dr._prune_dead_python_imports(str(tmp_path)) == ["pkg/__init__.py"]
    out = (tmp_path / "pkg/__init__.py").read_text()
    assert "from pkg.mod import C" in out
    assert "import C, helper" not in out


def test_an_all_entry_sharing_a_line_survives_the_prune(tmp_path):
    """Known limit: the __all__ cleanup is line-based, so it only removes an
    entry that sits on its own line. A single-line __all__ keeps the dead name,
    and `from pkg import *` still raises AttributeError for it."""
    _write(tmp_path, "pkg/mod.py", "class C:\n    def helper(self): pass\n")
    _write(tmp_path, "pkg/__init__.py",
           'from pkg.mod import C, helper\n\n__all__ = ["C", "helper"]\n')
    dr._prune_dead_python_imports(str(tmp_path))
    assert '__all__ = ["C", "helper"]' in (tmp_path / "pkg/__init__.py").read_text()


def test_a_clean_tree_is_left_alone(tmp_path):
    _write(tmp_path, "pkg/mod.py", "def helper(): pass\n")
    _write(tmp_path, "pkg/__init__.py", "from pkg.mod import helper\n")
    assert dr._prune_dead_python_imports(str(tmp_path)) == []


def test_an_unparseable_file_is_skipped_by_the_pruner(tmp_path):
    _write(tmp_path, "broken.py", "def (:\n")
    _write(tmp_path, "pkg/mod.py", "x = 1\n")
    assert dr._prune_dead_python_imports(str(tmp_path)) == []


def test_the_pruner_never_writes_source_that_stops_parsing(tmp_path, monkeypatch):
    _write(tmp_path, "mod.py", "def real(): pass\n")
    _write(tmp_path, "user.py", "from mod import real, gone\n")
    monkeypatch.setattr(dr, "_without_dead_names", lambda src, dead: "def (:\n")
    assert dr._prune_dead_python_imports(str(tmp_path)) == []
    assert (tmp_path / "user.py").read_text() == "from mod import real, gone\n"


# ─── the drift report ──────────────────────────────────────────────────


def test_a_missing_symbol_is_reported_with_the_closest_real_name():
    rows = dr._drift_rows({"expr": {"BinaryExpr", "UnaryExpr"}},
                          [("parser", "expr", "Binary")])
    assert rows == [{"consumer": "parser", "target": "expr", "name": "Binary",
                     "target_exposes": ["BinaryExpr", "UnaryExpr"],
                     "suggest": "BinaryExpr"}]


def test_a_symbol_that_exists_is_not_drift():
    assert dr._drift_rows({"expr": {"Binary"}}, [("parser", "expr", "Binary")]) == []


def test_no_close_match_suggests_nothing():
    rows = dr._drift_rows({"expr": {"Zebra"}}, [("parser", "expr", "Binary")])
    assert rows[0]["suggest"] is None


def test_the_exposed_list_is_capped():
    exposes = {"m": {f"name{i}" for i in range(40)}}
    rows = dr._drift_rows(exposes, [("c", "m", "missing")])
    assert len(rows[0]["target_exposes"]) == 15


def test_the_python_blackboard_pairs_exposes_with_consumes(tmp_path):
    _write(tmp_path, "expr.py", "class BinaryExpr: pass\n")
    _write(tmp_path, "parser.py", "from expr import Binary\n")
    exposes, consumes = dr._python_blackboard(str(tmp_path))
    assert exposes["expr"] == {"BinaryExpr"}
    assert ("parser", "expr", "Binary") in consumes


def test_the_blackboard_skips_unparseable_files(tmp_path):
    _write(tmp_path, "broken.py", "def (:\n")
    exposes, consumes = dr._python_blackboard(str(tmp_path))
    assert exposes == {}
    assert consumes == []


def test_star_imports_are_not_consumed_names(tmp_path):
    _write(tmp_path, "expr.py", "class C: pass\n")
    _write(tmp_path, "parser.py", "from expr import *\n")
    _exposes, consumes = dr._python_blackboard(str(tmp_path))
    assert consumes == []


def test_the_report_falls_back_to_the_ast_when_no_contracts_exist(tmp_path):
    _write(tmp_path, "expr.py", "class BinaryExpr: pass\n")
    _write(tmp_path, "parser.py", "from expr import Binary\n")
    rows = dr._symbol_drift_report(str(tmp_path))
    assert [r["name"] for r in rows] == ["Binary"]
    assert rows[0]["suggest"] == "BinaryExpr"


def test_declared_contracts_win_over_the_ast(monkeypatch, tmp_path):
    """Language-agnostic: the workers' own declared contracts describe a Java or
    Go tree the AST extractor cannot read at all."""
    _write(tmp_path, "expr.py", "class BinaryExpr: pass\n")
    monkeypatch.setattr(dr, "_blackboard_from_contracts",
                        lambda cwd: ({"Expr": {"BinaryExpr"}},
                                     [("Parser", "Expr", "Binary")]))
    rows = dr._symbol_drift_report(str(tmp_path))
    assert rows[0]["consumer"] == "Parser"
