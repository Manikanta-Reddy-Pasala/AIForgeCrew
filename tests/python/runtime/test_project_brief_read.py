"""Read-side brief assembly (audit R2/R4/R5).

project_brief_text(repo) = the repo brief ∪ its map_scopes-linked sibling briefs
(topic/global) ∪ the global brief — so a recalled project brief actually pulls
in what it's linked to, and the pipeline can inject the same as chat.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    return tmp_path


def _brief(md_store, key, facts, links=None):
    from aiforge_core.runtime import work_notes
    (md_store.brief_path(key)).write_text(
        work_notes.render_note("knowledge", key, title=f"{key}",
                               objective="Durable knowledge.", facts=facts,
                               links=links or [],
                               updated_at="2026-07-12T00:00:00+00:00"),
        encoding="utf-8")


def test_project_brief_unions_repo_linked_and_global(mem):
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import context_bundle
    _brief(md_store, "svc", ["svc retries 3x on NATS"],
           links=["[data-sync](compacted-data-sync.md)"])
    _brief(md_store, "data-sync", ["last-write-wins on updatedAt"])
    _brief(md_store, "shared", ["never commit directly to main"])

    out = context_bundle.project_brief_text("svc")
    assert "retries 3x on NATS" in out          # project
    assert "last-write-wins" in out             # LINKED topic brief (R5/R4)
    assert "never commit directly to main" in out  # global


def test_project_brief_no_repo_still_gives_global(mem):
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import context_bundle
    _brief(md_store, "shared", ["global only fact"])
    out = context_bundle.project_brief_text("")
    assert "global only fact" in out


def test_memory_block_prepends_project_brief(mem, monkeypatch):
    import types
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import memory_block
    _brief(md_store, "svc", ["svc uses NATS push sync"])
    _brief(md_store, "shared", ["never commit directly to main"])
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "used_sources": []})
    ticket = types.SimpleNamespace(title="t", body="b", identifier="ONE-1",
                                   project="svc")
    out = memory_block.fetch(ticket)
    assert "Project memory (OKR briefs)" in out
    assert "NATS push sync" in out
    assert "never commit directly to main" in out
