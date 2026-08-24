"""No double-storage: ingest_dir mirrors Phase-3 (kind=compacted, same source)
so a brief is ONE index row, not one knowledge + one compacted."""
from __future__ import annotations
import importlib
import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return tmp_path


def _rows(kind=None):
    from aiforge_core.memory import sqlite_memory as sm
    import sqlite3, os
    c = sqlite3.connect(os.environ["AIFORGE_MEMORY_DB_PATH"])
    if kind:
        return c.execute("SELECT COUNT(*) FROM memory_units WHERE kind=?", (kind,)).fetchone()[0]
    return c.execute("SELECT COUNT(*) FROM memory_units").fetchone()[0]


def test_ingest_dir_one_row_per_brief(mem):
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import work_notes
    (md_store.brief_path("svc")).write_text(
        work_notes.render_note("knowledge", "svc", title="svc",
                               objective="Durable.", facts=["fact one"],
                               updated_at="2026-07-12T00:00:00+00:00"),
        encoding="utf-8")
    md_store.ingest_dir()
    md_store.ingest_dir()   # re-run must NOT accumulate
    assert _rows("knowledge") == 1     # brief ingested under its real kind
    assert _rows() == 1               # not double-stored


def test_delete_stale_compacted_notes(mem):
    from aiforge_core.memory import sqlite_memory as sm
    sm.write_unit(text="old brief", kind="compacted", repo="notes",
                  source="compacted:x")
    sm.write_unit(text="live brief", kind="compacted", repo="svc",
                  source="compacted:y")
    assert sm.delete_stale_compacted_notes() == 1
    assert _rows("compacted") == 1


def test_write_content_dedup_no_duplicate_md(mem):
    from aiforge_core.memory import md_store
    a = md_store.write("same note", "identical body", kind="note", repo="svc")
    b = md_store.write("same note", "identical body", kind="note", repo="svc")
    files = list(md_store.captures_dir().glob("*.md"))
    assert len(files) == 1                 # no duplicate md file
    assert a["file"] == b["file"]


def test_embed_on_change_skips_reingest(mem):
    from aiforge_core.memory import md_store, sqlite_memory as sm
    md_store._ingest_unit(title="t", body="stable body", kind="compacted",
                          tags=[], source="compacted:x", repo="svc", replace=True)
    n1 = _rows()
    md_store._ingest_unit(title="t", body="stable body", kind="compacted",
                          tags=[], source="compacted:x", repo="svc", replace=True)
    assert _rows() == n1                   # unchanged → not re-inserted
