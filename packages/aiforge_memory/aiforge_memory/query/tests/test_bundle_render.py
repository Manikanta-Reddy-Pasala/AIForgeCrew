"""Unit tests for ContextBundle render + token-budget helpers.
No driver, no LLM, no sidecar."""
from __future__ import annotations

from aiforge_memory.query.bundle import ContextBundle, _count_tokens


def test_render_includes_conventions_when_set() -> None:
    b = ContextBundle(repo="r", conventions_md="- use ruff\n- pin deps\n")
    out = b.render()
    assert "## Conventions (.cursorrules)" in out
    assert "use ruff" in out


def test_render_skips_conventions_when_empty() -> None:
    out = ContextBundle(repo="r").render()
    assert "## Conventions" not in out


def test_render_includes_repo_map_when_set() -> None:
    b = ContextBundle(repo="r", repo_map="api/main.py:\n  - process\n")
    out = b.render()
    assert "## Repo Map" in out
    assert "api/main.py" in out


def test_render_includes_chunks_with_path_and_text() -> None:
    b = ContextBundle(
        repo="r",
        chunks=[
            {"file_path": "a.py", "text": "def f(): pass"},
            {"file_path": "b.py", "text": "x = 1"},
        ],
    )
    out = b.render()
    assert "## Relevant Code Chunks" in out
    assert "### `a.py`" in out
    assert "def f(): pass" in out
    assert "### `b.py`" in out


def test_render_caps_chunks_at_5() -> None:
    chunks = [{"file_path": f"{i}.py", "text": f"chunk{i}"} for i in range(10)]
    out = ContextBundle(repo="r", chunks=chunks).render()
    assert "chunk0" in out
    assert "chunk4" in out
    assert "chunk5" not in out  # 6th chunk dropped


def test_render_skips_chunks_with_missing_fields() -> None:
    b = ContextBundle(
        repo="r",
        chunks=[
            {"file_path": "", "text": "no path"},
            {"file_path": "ok.py", "text": ""},
            {"file_path": "good.py", "text": "real"},
        ],
    )
    out = b.render()
    assert "no path" not in out
    assert "good.py" in out


def test_render_includes_notes() -> None:
    b = ContextBundle(repo="r", notes=[
        {"title": "Migration", "body": "v2 schema needs reindex"},
    ])
    out = b.render()
    assert "## Notes" in out
    assert "Migration" in out
    assert "v2 schema" in out


def test_render_includes_docs_with_url() -> None:
    b = ContextBundle(repo="r", docs=[
        {"title": "Neo4j Vector", "url": "https://neo4j.com/v", "body": "cosine"},
    ])
    out = b.render()
    assert "## External Docs" in out
    assert "Neo4j Vector" in out
    assert "https://neo4j.com/v" in out


def test_render_truncates_long_runbook() -> None:
    b = ContextBundle(repo="r", runbook_md="x" * 5000)
    out = b.render()
    # 2000-char cap on runbook section
    assert out.count("x") == 2000


def test_count_tokens_returns_positive_int() -> None:
    n = _count_tokens("hello world this is a sentence")
    assert isinstance(n, int)
    assert n > 0


def test_count_tokens_empty_string() -> None:
    assert _count_tokens("") == 0


def test_count_tokens_grows_with_input() -> None:
    short = _count_tokens("short")
    long = _count_tokens("short " * 200)
    assert long > short * 50
