"""Data-backend resolution + guard for the SQLite-only build.

Postgres/Neo4j were removed, so every store resolves to embedded SQLite and
the ``require_data_backends`` guard is a no-op — there is no external backend
to require.
"""
from __future__ import annotations

from aiforge_core.config import backends


def test_resolve_backends_shape():
    b = backends.resolve_backends()
    assert set(b) == {"tickets", "memory", "chat", "jobs"}
    assert b["tickets"] == "sqlite"
    assert b["chat"] == "sqlite"
    assert b["jobs"] == "sqlite"
    assert b["memory"] == "sqlite"


def test_guard_is_noop(monkeypatch):
    # Even with the (legacy) require flag set, SQLite-only never raises.
    monkeypatch.setenv("AIFORGE_REQUIRE_DATA_BACKEND", "1")
    backends.require_data_backends()  # must not raise


def test_boot_log_never_raises():
    backends.boot_log()
