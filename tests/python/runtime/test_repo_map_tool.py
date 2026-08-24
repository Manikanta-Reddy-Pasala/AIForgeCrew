"""Tests for the repo_map tool — pure tree-sitter AST repo map (no vectors).

The grep+AST navigation path: exact, zero-staleness, no embedding /
re-index cost. Complements grep_repo (exact pattern) + graphify_lookup
(typed edges).
"""
from __future__ import annotations

import textwrap

from aiforge_core.runtime import doer_tools


def _seed_repo(tmp_path):
    (tmp_path / "calc.py").write_text(textwrap.dedent('''
        """A small module so the repo map has real symbols to rank."""

        class Calculator:
            def add(self, a, b):
                return a + b

            def multiply(self, a, b):
                return a * b


        def run_calc():
            c = Calculator()
            return c.add(1, 2) + c.multiply(3, 4)
    '''))
    (tmp_path / "main.py").write_text(textwrap.dedent('''
        from calc import run_calc

        def main():
            print(run_calc())
    '''))
    # Aider's RepoMap skips repos with < 5 files — seed a few more so the
    # tree-sitter digest actually renders.
    for i in range(4):
        (tmp_path / f"mod{i}.py").write_text(textwrap.dedent(f'''
            def helper_{i}(x):
                """Filler symbol {i} so the map has files to rank."""
                return x + {i}
        '''))


def test_repo_map_returns_treesitter_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("AIFORGE_AIDER_REPOMAP_ENABLED", "1")
    _seed_repo(tmp_path)
    out = doer_tools.repo_map(focus="calculator add multiply", token_budget=512)
    assert out["ok"] is True, out
    assert out["engine"].startswith("aider-treesitter-pagerank")
    digest = out["digest"]
    # ranked symbols from the seeded files should surface
    assert "calc.py" in digest
    assert "Calculator" in digest


def test_repo_map_empty_repo_fails_gracefully(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    out = doer_tools.repo_map(focus="anything")
    # no source files → ok False, never raises
    assert out["ok"] is False
    assert "digest" in out
    assert out["digest"] == ""


def test_repo_map_in_doer_tool_list() -> None:
    names = [t.name for t in doer_tools.adk_function_tools()]
    assert "repo_map" in names
    # the grep+AST trio is all present
    assert "grep_repo" in names
    assert "graphify_lookup" in names
    assert "impacted_tests" in names


def test_digest_file_paths_parser() -> None:
    digest = textwrap.dedent('''
        aiforge_core/runtime/pipeline.py:
        │def build_pipeline(...):
        ⋮
        aiforge_core/agents/doer.py:
        │class Doer:
    ''')
    paths = doer_tools._digest_file_paths(digest)
    assert "aiforge_core/runtime/pipeline.py" in paths
    assert "aiforge_core/agents/doer.py" in paths
    # code lines (│ / ⋮ prefixed) are NOT mistaken for paths
    assert all(not p.startswith(("│", "⋮")) for p in paths)


def test_impacted_tests_no_files() -> None:
    out = doer_tools.impacted_tests("")
    assert out["ok"] is False
    assert out["tests"] == []


def test_impacted_tests_no_repo_soft_fails(monkeypatch) -> None:
    monkeypatch.delenv("AIFORGE_AFM_REPO", raising=False)
    out = doer_tools.impacted_tests("src/Foo.java")
    assert out["ok"] is False
    assert "AIFORGE_AFM_REPO" in out["error"]
    assert out["tests"] == []


def test_impacted_tests_repo_set_no_backend(monkeypatch) -> None:
    # repo set but Neo4j unreachable → soft-fail to empty list (run all)
    monkeypatch.setenv("AIFORGE_AFM_REPO", "SomeRepo")
    monkeypatch.setenv("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:59999")
    out = doer_tools.impacted_tests("src/Foo.java, src/Bar.java")
    # either ok with empty tests, or graceful error — never raises
    assert out["tests"] == []
