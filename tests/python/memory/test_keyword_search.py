"""Hybrid search: FTS5 keyword (BM25) + spell correction."""
from __future__ import annotations
import importlib
import pytest


@pytest.fixture()
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import aiforge_core.memory.local_embed as le
    monkeypatch.setattr(le, "embed", lambda t: [1.0, 0.0])
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return sm


def test_keyword_exact_id(sm):
    sm.write_unit(text="ONE-3 first end-to-end pipeline green", kind="learning", repo="svc")
    sm.write_unit(text="ONE-2 tally routing bug", kind="learning", repo="svc")
    hits = sm.keyword_search("ONE-3", repo="svc")
    assert hits and "ONE-3" in hits[0]["text"]
    assert all("ONE-2" not in h["text"] for h in hits[:1])


def test_keyword_prefix(sm):
    sm.write_unit(text="OrderController maps GET /orders", kind="learning", repo="svc")
    hits = sm.keyword_search("OrderControl", repo="svc")   # prefix
    assert hits and "OrderController" in hits[0]["text"]


def test_keyword_spell_correction(sm):
    sm.write_unit(text="sync retries three times on NATS timeout", kind="learning", repo="svc")
    hits = sm.keyword_search("retres", repo="svc")   # typo → retries
    assert hits and "retries" in hits[0]["text"]


def test_keyword_scope_filter(sm):
    sm.write_unit(text="branch naming convention feature slash", kind="learning", repo="shared")
    sm.write_unit(text="branch other repo secret", kind="learning", repo="other")
    hits = sm.keyword_search("branch naming", repo="svc")
    txt = " | ".join(h["text"] for h in hits)
    assert "convention" in txt          # shared/global surfaces
    assert "other repo secret" not in txt
