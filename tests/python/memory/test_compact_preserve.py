"""Compaction preservation (audit COMPACTING P1) — KR/Links/Learnings union-back
and sweep_empty keeping a links-only brief."""
from __future__ import annotations
import pytest


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    return tmp_path


def test_union_back_appends_missing():
    from aiforge_core.memory.md_store import _union_back
    assert _union_back(["a"], ["a", "b"]) == ["a", "b"]
    assert _union_back(None, ["x"]) == ["x"]
    assert _union_back(["k"], None) == ["k"]


def test_sweep_empty_keeps_links_only_brief(mem):
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import work_notes
    (md_store.memory_dir() / "compacted-svc.md").write_text(
        work_notes.render_note("knowledge", "svc", title="svc",
                               objective="Durable knowledge.",
                               links=["[global](compacted-shared.md)"],
                               updated_at="2026-07-12T00:00:00+00:00"),
        encoding="utf-8")
    md_store.sweep_empty_briefs(archive=True)
    assert (md_store.memory_dir() / "compacted-svc.md").exists()  # links = content
