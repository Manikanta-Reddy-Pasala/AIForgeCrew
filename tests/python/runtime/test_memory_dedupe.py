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
