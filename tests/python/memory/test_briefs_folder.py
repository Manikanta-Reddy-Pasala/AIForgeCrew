"""Briefs live in the compacted/ subfolder; migration must be non-destructive."""
from __future__ import annotations
import importlib
import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return tmp_path


def test_brief_upsert_writes_into_compacted_folder(mem):
    from aiforge_core.memory import md_store as m
    m._brief_upsert("svc", "db access only via gateway", topic="arch")
    assert m.brief_path("svc").exists()
    assert m.brief_path("svc").parent == m.briefs_dir()
    assert not (m.memory_dir() / "compacted-svc.md").exists()   # not in root


def test_migrate_does_not_delete_briefs_already_in_folder(mem):
    """Regression: migrate_briefs_to_folder iterating iter_briefs() deleted the
    live briefs (dest==source → unlink). It must be a no-op for foldered briefs."""
    from aiforge_core.memory import md_store as m
    m._brief_upsert("svc", "a durable fact", topic="x")
    m._brief_upsert("shared", "a global fact")
    before = {p.name for p in m.briefs_dir().glob("compacted-*.md")}
    assert before == {"compacted-svc.md", "compacted-shared.md"}

    r = m.migrate_briefs_to_folder()
    after = {p.name for p in m.briefs_dir().glob("compacted-*.md")}
    assert after == before                              # nothing deleted/moved
    assert r["moved"] == 0
    # idempotent across repeated startups
    m.migrate_briefs_to_folder()
    assert {p.name for p in m.briefs_dir().glob("compacted-*.md")} == before


def test_migrate_moves_legacy_root_brief_into_folder(mem):
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import work_notes
    # a legacy brief sitting in the root (pre-folder layout)
    (m.memory_dir() / "compacted-legacy.md").write_text(
        work_notes.render_note("knowledge", "legacy", title="Legacy",
                               facts=["old fact"],
                               updated_at="2026-07-12T00:00:00+00:00"),
        encoding="utf-8")
    r = m.migrate_briefs_to_folder()
    assert r["moved"] == 1
    assert m.brief_path("legacy").exists()                       # now in folder
    assert not (m.memory_dir() / "compacted-legacy.md").exists()  # gone from root


def test_clear_md_wipes_foldered_briefs(mem):
    """Regression: clear_store('md_files') / --clear must wipe the compacted/
    briefs too — a root-only glob left stale briefs after a --clear re-ingest."""
    from aiforge_core.memory import md_store as m
    from aiforge_core.memory import admin
    m._brief_upsert("svc", "a fact", topic="x")
    m.write("a capture", "some body", kind="note", repo="svc")   # root note
    assert m.brief_path("svc").exists()
    admin.clear_store("md_files")
    assert not m.brief_path("svc").exists()                       # brief gone
    assert list(m.briefs_dir().glob("*.md")) == []
    assert list(m.memory_dir().glob("*.md")) == []                # root gone too


def test_startup_migrations_preserve_foldered_briefs(mem):
    """The whole startup path must not lose a brief in the compacted/ folder."""
    from aiforge_core.memory import md_store as m
    from aiforge_core.memory import migrations
    m._brief_upsert("svc", "deploy via git pull then restart", topic="deploy")
    assert m.brief_path("svc").exists()
    migrations.run_startup_migrations()
    assert m.brief_path("svc").exists()                          # survived
