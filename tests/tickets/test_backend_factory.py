import importlib
import os

import pytest


def _reload_env(monkeypatch, **env):
    for k in ("AIFORGE_PG_URL", "AIFORGE_DB_PATH", "AIFORGE_FORCE_PG"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import aiforge_core.config.env as envmod
    return importlib.reload(envmod)


def test_default_is_sqlite(monkeypatch):
    envmod = _reload_env(monkeypatch)
    assert envmod.AIFORGE_USE_SQLITE is True
    assert envmod.AIFORGE_PG_URL is None


def test_pg_url_selects_postgres(monkeypatch):
    envmod = _reload_env(monkeypatch, AIFORGE_PG_URL="postgresql://x/y")
    assert envmod.AIFORGE_USE_SQLITE is False
    assert envmod.AIFORGE_PG_URL == "postgresql://x/y"


def test_both_backends_satisfy_protocol():
    from aiforge_core.tickets.backends.base import StoreBackend
    from aiforge_core.tickets.backends.pg_backend import PgBackend
    from aiforge_core.tickets.backends.sqlite_backend import SqliteBackend
    proto = [m for m in dir(StoreBackend) if not m.startswith("_")]
    for be in (PgBackend, SqliteBackend):
        missing = [m for m in proto if not hasattr(be, m)]
        assert missing == [], f"{be.__name__} missing {missing}"
