"""Committed agent-instruction (CLAUDE.md/AGENTS.md/…) → OKR memory ingest."""
from __future__ import annotations
import importlib
import pytest


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return tmp_path


def test_split_sections_by_heading():
    from aiforge_core.memory import instructions_ingest as ii
    md = "preamble text\n## Build\nrun make\n### Deploy\nkubectl apply\n"
    secs = ii._split_sections(md)
    heads = [h for h, _ in secs]
    assert heads == ["", "Build", "Deploy"]
    assert secs[1] == ("Build", "run make")


def test_ingest_creates_briefs_and_is_searchable(mem, tmp_path):
    from aiforge_core.memory import instructions_ingest as ii
    from aiforge_core.memory import sqlite_memory as sm
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text(
        "# My Repo\n"
        "## Build Commands\nRun `make build` to compile the service.\n"
        "## Deployment\nTag a release then push to trigger the pipeline.\n",
        encoding="utf-8")

    res = ii.ingest_instruction_files([str(repo / "CLAUDE.md")], compact=False)
    assert res["ok"] and res["files"] == 1
    assert res["captured"] >= 2                       # both sections captured
    assert any("make build" in (h.get("text") or "")
               for h in sm.recall("compile the service", limit=8))


def test_clear_then_ingest_is_deterministic(mem, tmp_path):
    from aiforge_core.memory import instructions_ingest as ii, md_store, sqlite_memory as sm
    md_store.capture("learning", "stale unrelated fact about widgets", repo="old")
    repo = tmp_path / "svc"
    (repo / ".git").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text(
        "## Overview\nThe svc handles payments end to end.\n", encoding="utf-8")

    res = ii.ingest_instruction_files([str(repo / "CLAUDE.md")], clear=True,
                                      compact=False)
    assert res["ok"] and res["cleared"] is not None
    hits = " ".join(h.get("text") or "" for h in sm.recall("widgets", limit=8))
    assert "widgets" not in hits
    assert any("payments" in (h.get("text") or "")
               for h in sm.recall("payments", limit=8))


def test_discover_scans_tree_multiple_names_skips_vendor(tmp_path):
    from aiforge_core.memory import instructions_ingest as ii
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "CLAUDE.md").write_text("## X\nbody\n", encoding="utf-8")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "AGENTS.md").write_text("## Y\nbody\n", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "CLAUDE.md").write_text("nope\n",
                                                                 encoding="utf-8")
    found = [str(p) for p in ii.discover([str(tmp_path)])]
    assert any("/a/CLAUDE.md" in f for f in found)
    assert any("/b/AGENTS.md" in f for f in found)          # multi-name discovery
    assert not any("node_modules" in f for f in found)
