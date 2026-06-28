"""K4 — central Neo4j connection helper env-fallback chain."""
from __future__ import annotations

from aiforge_memory.core.neo4j import neo4j_settings, open_driver


def _clear(monkeypatch):
    for k in ("AIFORGE_NEO4J_URI", "AIFORGE_NEO4J_USER",
              "AIFORGE_NEO4J_PASSWORD",
              "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(k, raising=False)


def test_defaults_when_no_env(monkeypatch):
    _clear(monkeypatch)
    assert neo4j_settings() == ("bolt://127.0.0.1:7687", "neo4j", "password")


def test_plain_neo4j_env_is_fallback(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("NEO4J_URI", "bolt://other:7687")
    monkeypatch.setenv("NEO4J_USER", "u2")
    monkeypatch.setenv("NEO4J_PASSWORD", "p2")
    assert neo4j_settings() == ("bolt://other:7687", "u2", "p2")


def test_aiforge_env_wins_over_plain(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("NEO4J_URI", "bolt://other:7687")
    monkeypatch.setenv("AIFORGE_NEO4J_URI", "bolt://primary:7687")
    uri, user, pw = neo4j_settings()
    assert uri == "bolt://primary:7687"


def test_open_driver_uses_settings(monkeypatch):
    _clear(monkeypatch)
    # Driver construction is lazy — no connection is attempted here.
    drv = open_driver()
    try:
        assert drv is not None
    finally:
        drv.close()
