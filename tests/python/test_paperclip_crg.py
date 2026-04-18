from __future__ import annotations

from pathlib import Path

import pytest

from paperclip.crg import blast_radius, build_graph, dependency_chain


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "def foo():\n    return 1\n\n"
        "def bar():\n    return foo() + 2\n"
    )
    (tmp_path / "src" / "b.py").write_text(
        "from src.a import bar\n\n"
        "def baz():\n    return bar() * 3\n"
    )
    return tmp_path


def test_build_graph_finds_functions(tiny_repo: Path) -> None:
    g = build_graph(tiny_repo)
    assert "src/a.py::foo" in g.symbols
    assert "src/a.py::bar" in g.symbols
    assert "src/b.py::baz" in g.symbols


def test_blast_radius_up_one_level(tiny_repo: Path) -> None:
    g = build_graph(tiny_repo)
    r = blast_radius(g, "src/a.py::foo", max_depth=1)
    # foo called by bar (in a.py). baz calls bar but is 2 hops away.
    assert "src/a.py::bar" in r["affected_symbols"]
    assert "src/a.py" in r["files"]


def test_blast_radius_two_levels(tiny_repo: Path) -> None:
    g = build_graph(tiny_repo)
    r = blast_radius(g, "src/a.py::foo", max_depth=3)
    # At depth≥2, baz (which calls bar which calls foo) should appear.
    assert "src/b.py::baz" in r["affected_symbols"]
    assert "src/b.py" in r["files"]


def test_dependency_chain(tiny_repo: Path) -> None:
    g = build_graph(tiny_repo)
    r = dependency_chain(g, "src/a.py::bar")
    assert any("foo" in c for c in r["callees"])
    assert any("baz" in c for c in r["callers"])


def test_unknown_target(tiny_repo: Path) -> None:
    g = build_graph(tiny_repo)
    r = blast_radius(g, "does/not/exist.py::nope")
    assert r["files"] == []
    assert "not found" in r["note"]
