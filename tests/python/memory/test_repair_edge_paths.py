"""The branches where repair could lose something.

repair_captures() deletes and rewrites files in a store a human also writes to
by hand, so its guards — the concurrent-write re-check, the archive-before-
collapse, the unreadable note, the --delete path — are the code that matters
most and were the least exercised.
"""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", tempfile.mkdtemp() + "/m.db")
    return None


def _junk(m, title="Final"):
    return m.write(title, "Final", kind="topic_learning", repo="svc")


def test_a_note_that_became_good_under_us_is_kept(cfg, monkeypatch):
    """A chat turn can fold a real claim into the file between the scan and the
    move. Archiving it then would take that fact with it."""
    from aiforge_core.memory import md_store as m
    from aiforge_core.memory.md_store import _repair

    _junk(m)
    path = next(iter(m.captures_dir().glob("final-*.md")))
    real = _repair._parse

    def _rewrite_then_parse(p):
        # simulate the concurrent write, once, at re-check time
        if p == path and "MongoDbService" not in p.read_text(encoding="utf-8"):
            p.write_text(p.read_text(encoding="utf-8").replace(
                "Final\n", "MongoDbService is the mandatory gateway.\n"), encoding="utf-8")
        return real(p)

    monkeypatch.setattr(_repair, "_parse", _rewrite_then_parse)
    out = m.repair_captures()
    assert out["retired"] == 0
    assert path.exists(), "a note that became good was archived anyway"


def test_delete_mode_removes_the_file_and_leaves_no_archive(cfg):
    from aiforge_core.memory import md_store as m
    _junk(m)
    out = m.repair_captures(archive=False)
    assert out["retired"] == 1 and out["archived"] is False
    assert not list(m.captures_dir().glob("final-*.md"))
    assert not list((m.memory_dir() / "archive").rglob("*.md"))


def test_limit_stops_the_scan(cfg):
    from aiforge_core.memory import md_store as m
    for i in range(5):
        _junk(m, title=f"Final {i}")
    out = m.repair_captures(limit=2)
    assert out["scanned"] == 2
    assert out["retired"] <= 2


def test_an_unreadable_note_is_skipped_not_retired(cfg, monkeypatch):
    from aiforge_core.memory import md_store as m
    from aiforge_core.memory.md_store import _repair

    _junk(m)
    path = next(iter(m.captures_dir().glob("final-*.md")))
    monkeypatch.setattr(_repair, "_parse",
                        lambda p: (_ for _ in ()).throw(OSError("bad sector")))
    out = m.repair_captures()
    assert out["ok"] and out["retired"] == 0
    assert path.exists()


def test_a_collapse_that_cannot_be_archived_is_not_performed(cfg, monkeypatch):
    """No copy, no rewrite — dropping a claim in place is not recoverable."""
    from aiforge_core.memory import md_store as m
    from aiforge_core.memory.md_store import _repair

    m.write("clear lockout", "- clear lockout takes a cphash and\n"
                             "- clear lockout takes a cphash and a setup value",
            kind="learning", repo="svc")
    path = next(iter(m.captures_dir().glob("clear-lockout-*.md")))
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(_repair.shutil, "copy2",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    out = m.repair_captures()
    assert out["collapsed"] == 0
    assert path.read_text(encoding="utf-8") == before


def test_an_empty_note_is_retired(cfg):
    from aiforge_core.memory import md_store as m
    m.write("blank", "", kind="learning", repo="svc")
    out = m.repair_captures()
    assert out["retired"] == 1
    assert out["files"][0]["reasons"] == ["empty note"]


def test_the_pass_reports_a_failure_rather_than_raising(cfg, monkeypatch):
    from aiforge_core.memory import md_store as m
    from aiforge_core.memory.md_store import _repair

    monkeypatch.setattr(_repair, "_capture_md_files",
                        lambda: (_ for _ in ()).throw(RuntimeError("disk gone")))
    out = m.repair_captures()
    assert out["ok"] is False and "disk gone" in out["error"]


def test_a_note_of_a_kind_repair_does_not_own_is_never_judged(cfg):
    """Only per-fact captures. A rule file or a hand-dropped page is not this
    pass's business, whatever its body looks like."""
    from aiforge_core.memory import md_store as m
    m.write("rules", "## House rules\n- never tag a release\n",
            kind="rule", repo="svc")
    out = m.repair_captures()
    assert out["retired"] == 0
    assert list(m.captures_dir().glob("rules-*.md"))
