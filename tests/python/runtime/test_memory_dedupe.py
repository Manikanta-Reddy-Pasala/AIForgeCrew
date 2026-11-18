"""Periodic semantic dedup — collapses near-duplicate memory units (cosine on
stored vectors), keeps the newest, preserves preferences.
"""
from __future__ import annotations

import json
import tempfile

import pytest


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", tempfile.mkdtemp() + "/m.db")
    return None


def _insert(kind, text, vec, repo="r"):
    from aiforge_core.memory import sqlite_memory as m
    with m._LOCK, m._conn() as c:
        c.execute(
            "INSERT INTO memory_units (kind, source, title, text, tags, metadata,"
            " repo, ticket, embedding) VALUES (?,?,?,?,?,?,?,?,?)",
            (kind, "t", "", text, "[]", "{}", repo, None, json.dumps(vec)))


def test_dedupe_collapses_near_duplicate_vectors(cfg):
    from aiforge_core.memory import sqlite_memory as m
    v = [1.0, 0.0, 0.0]
    _insert("note", "fact one", v)
    _insert("note", "fact one paraphrased", [0.999, 0.001, 0.0])   # near-dup
    _insert("note", "totally different", [0.0, 1.0, 0.0])          # distinct
    r = m.dedupe(threshold=0.95)
    assert r["removed"] == 1
    with m._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM memory_units").fetchone()[0]
    assert n == 2


def test_dedupe_preserves_preferences(cfg):
    from aiforge_core.memory import sqlite_memory as m
    v = [1.0, 0.0, 0.0]
    _insert("preference", "pref one", v)
    _insert("preference", "pref two", [0.999, 0.001, 0.0])   # near-dup BUT pref
    r = m.dedupe(threshold=0.95)
    assert r["removed"] == 0                                  # prefs untouched
    with m._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM memory_units "
                      "WHERE kind='preference'").fetchone()[0]
    assert n == 2


def test_dedupe_different_kind_not_merged(cfg):
    from aiforge_core.memory import sqlite_memory as m
    v = [1.0, 0.0, 0.0]
    _insert("note", "x", v)
    _insert("decision", "x", v)          # same vector, different kind → both kept
    r = m.dedupe(threshold=0.9)
    assert r["removed"] == 0


def test_reworded_copies_of_one_fact_merge_by_words(cfg):
    """Same wording (>= 90% shared words), same kind and repo: one fact, even
    when the embeddings are far apart; a different repo keeps its own copy."""
    from aiforge_core.memory import sqlite_memory as m
    _insert("note", "the settlement batch runs after the regional ledger closes each day", [1.0, 0.0, 0.0])
    _insert("note", "The settlement batch runs after the regional ledger closes, each day.", [0.0, 1.0, 0.0])
    _insert("note", "the settlement batch runs after the regional ledger closes each day", [0.0, 0.0, 1.0], repo="other")
    _insert("note", "refunds go through the payment gateway", [0.5, 0.5, 0.0])
    r = m.dedupe(threshold=0.95)
    assert r["removed"] == 1
    with m._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM memory_units").fetchone()[0] == 3


def test_compact_storage_shortens_vectors_and_keeps_them_usable(cfg):
    from aiforge_core.memory import sqlite_memory as m
    from aiforge_core.memory.sqlite_memory._vecfmt import vec_json
    long_vec = [0.123456789012345, -0.0000123456789, 0.987654321098765]
    _insert("note", "a fact", long_vec)
    r = m.compact_storage()
    assert r["rewritten"] == 1 and r["vacuumed"] is True
    with m._conn() as c:
        stored = c.execute("SELECT embedding FROM memory_units").fetchone()[0]
    assert stored == vec_json(long_vec) == "[0.123457,-1.23457e-05,0.987654]"
    assert max(abs(a - b) for a, b in zip(json.loads(stored), long_vec)) < 1e-6
    assert m.compact_storage()["rewritten"] == 0        # idempotent
