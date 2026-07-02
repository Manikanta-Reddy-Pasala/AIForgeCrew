"""Data-driven backend guard + boot resolution.

The guard is the fail-loud contract: in docker/hybrid the run script exports
``AIFORGE_REQUIRE_DATA_BACKEND=1`` and any store that resolves to embedded
SQLite must abort boot with a clear message. ``--lite`` never sets the flag,
so zero-infra users are untouched.
"""
from __future__ import annotations

import pytest

from aiforge_core.config import backends
from aiforge_core.config import env


def _sqlite_env(monkeypatch):
    monkeypatch.setattr(env, "AIFORGE_USE_SQLITE", True, raising=False)
    monkeypatch.setattr(env, "AIFORGE_PG_URL", None, raising=False)
    monkeypatch.delenv("AIFORGE_MEMORY_BACKEND", raising=False)
    monkeypatch.delenv("AIFORGE_NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)


def _data_env(monkeypatch):
    monkeypatch.setattr(env, "AIFORGE_USE_SQLITE", False, raising=False)
    monkeypatch.setattr(env, "AIFORGE_PG_URL",
                        "postgresql://u@127.0.0.1:5432/db", raising=False)
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "neo4j")


def test_resolve_backends_shape(monkeypatch):
    _sqlite_env(monkeypatch)
    b = backends.resolve_backends()
    assert set(b) == {"tickets", "memory", "chat", "jobs"}
    assert b["tickets"] == "sqlite"
    assert b["chat"] == "sqlite"
    assert b["jobs"] == "sqlite"
    assert b["memory"] == "sqlite"


def test_resolve_backends_data_mode(monkeypatch):
    _data_env(monkeypatch)
    b = backends.resolve_backends()
    assert b["tickets"] == "postgres"
    assert b["chat"] == "postgres"
    assert b["jobs"] == "postgres"
    assert b["memory"] == "neo4j"


def test_guard_raises_when_required_and_sqlite(monkeypatch):
    monkeypatch.setenv("AIFORGE_REQUIRE_DATA_BACKEND", "1")
    _sqlite_env(monkeypatch)
    with pytest.raises(RuntimeError) as exc:
        backends.require_data_backends()
    msg = str(exc.value)
    assert "--lite" in msg
    assert "AIFORGE_PG_URL" in msg


def test_guard_ok_when_pg_and_neo4j(monkeypatch):
    monkeypatch.setenv("AIFORGE_REQUIRE_DATA_BACKEND", "1")
    _data_env(monkeypatch)
    backends.require_data_backends()  # must not raise


def test_guard_noop_when_flag_unset(monkeypatch):
    monkeypatch.delenv("AIFORGE_REQUIRE_DATA_BACKEND", raising=False)
    _sqlite_env(monkeypatch)
    backends.require_data_backends()  # lite: sqlite allowed, no raise


def test_boot_log_never_raises(monkeypatch):
    _sqlite_env(monkeypatch)
    # even if resolution blows up, boot_log swallows it
    backends.boot_log()
