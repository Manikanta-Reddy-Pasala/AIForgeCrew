"""Markdown-file memory store: write/list/read/ingest + frontmatter."""
import json as _json  # noqa: F401

import pytest


@pytest.fixture
def md(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import importlib

    from aiforge_core.memory import md_store
    importlib.reload(md_store)
    return md_store


def test_write_creates_file_with_frontmatter(md):
    d = md.write("My Title", "the body text", kind="session", tags=["a", "b"])
    assert d["title"] == "My Title" and d["kind"] == "session"
    path = md.memory_dir() / d["file"]
    raw = path.read_text()
    assert raw.startswith("---") and "title: My Title" in raw
    assert "the body text" in raw


def test_list_and_read(md):
    md.write("Note one", "body one")
    md.write("Note two", "body two")
    names = {f["title"] for f in md.list_files()}
    assert {"Note one", "Note two"} <= names
    first = md.list_files()[0]
    full = md.read_file(first["name"])
    assert full["body"]


def test_ingest_dir_picks_up_handwritten(md):
    # drop a raw md file by hand
    (md.memory_dir() / "manual.md").write_text(
        "---\ntitle: Hand note\nkind: gotcha\n---\n\nremember this\n")
    out = md.ingest_dir()
    assert out["ok"] and out["ingested"] >= 1
    assert any(f["title"] == "Hand note" for f in md.list_files())


def test_read_missing_returns_none(md):
    assert md.read_file("nope") is None


def test_delete(md):
    d = md.write("ToDelete", "x")
    assert md.delete_file(d["name"]) is True
    assert md.read_file(d["name"]) is None
