"""Seed-TOC concept index, graph-health linter, and hot-cache recent source."""
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


# ── Part 1: seed TOC / concept index ─────────────────────────────────────────

def test_brief_index_and_seed_block(mem):
    from aiforge_core.memory import md_store as m
    m._brief_upsert("svc", "deploy via git pull then restart")
    m._brief_upsert("shared", "always pin dependency versions")
    idx = m.brief_index()
    keys = {d["key"] for d in idx}
    assert {"svc", "shared"} <= keys
    svc = next(d for d in idx if d["key"] == "svc")
    assert "git pull" in svc["snippet"]

    block = m.seed_memory_block()
    assert "memory index" in block.lower()
    assert "svc:" in block and "shared:" in block


def test_seed_block_disabled(mem, monkeypatch):
    from aiforge_core.memory import md_store as m
    m._brief_upsert("svc", "a fact for svc here")
    monkeypatch.setenv("AIFORGE_SEED_TOC", "0")
    assert m.seed_memory_block() == ""


# ── Part 2: graph-health linter ──────────────────────────────────────────────

def _brief_with_links(m, key, facts, links):
    from aiforge_core.runtime import work_notes
    m.briefs_dir().mkdir(parents=True, exist_ok=True)
    m.brief_path(key).write_text(
        work_notes.render_note("knowledge", key, title=key.title(),
                               facts=facts, links=links,
                               updated_at="2026-07-13T00:00:00+00:00"),
        encoding="utf-8")


def test_lint_finds_broken_link_and_orphan(mem):
    from aiforge_core.memory import md_store as m
    # a links to b (exists) and to ghost (missing); c is an orphan
    _brief_with_links(m, "a", ["fact a"],
                      ["[B](compacted-b.md)", "[Ghost](compacted-ghost.md)"])
    _brief_with_links(m, "b", ["fact b"], ["[A](compacted-a.md)"])
    _brief_with_links(m, "c", ["fact c"], [])
    r = m.lint_graph(repair=False)
    assert {"brief": "a", "ref": "compacted-ghost.md"} in r["broken"]
    assert "c" in r["orphans"]
    assert "a" not in r["orphans"] and "b" not in r["orphans"]  # linked pair


def test_lint_repair_strips_broken_ref(mem):
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import work_notes
    _brief_with_links(m, "a", ["fact a"],
                      ["[B](compacted-b.md)", "[Ghost](compacted-ghost.md)",
                       "https://example.com/doc"])
    _brief_with_links(m, "b", ["fact b"], ["[A](compacted-a.md)"])
    r = m.lint_graph(repair=True)
    assert r["repaired"] == 1
    links = work_notes.parse_note(
        m.brief_path("a").read_text())["sections"]["links"]
    assert "[Ghost](compacted-ghost.md)" not in links      # dangling stripped
    assert "[B](compacted-b.md)" in links                  # valid kept
    assert "https://example.com/doc" in links              # url kept


# ── Part 3: hot-cache recent source ──────────────────────────────────────────

def test_recent_returns_newest_first(mem):
    from aiforge_core.memory import sqlite_memory as sm
    sm.write_unit(text="oldest fact one", kind="learning", repo="svc",
                  source="s1")
    sm.write_unit(text="newest fact two", kind="learning", repo="svc",
                  source="s2")
    rows = sm.recent(limit=5, repo="svc")
    assert rows and rows[0]["text"] == "newest fact two"    # newest first
    assert rows[0]["score"] >= rows[-1]["score"]


def test_unified_query_includes_recent_source(mem):
    from aiforge_core.memory import sqlite_memory as sm, unified_query as uq
    sm.write_unit(text="freshly captured deploy note about nucbox restart",
                  kind="learning", repo="svc", source="fresh")
    res = uq.query("something totally unrelated xyzzy", repo="svc", limit=5)
    assert "recent" in res["used_sources"]
    assert any("freshly captured" in (h.get("text") or "") for h in res["hits"])
