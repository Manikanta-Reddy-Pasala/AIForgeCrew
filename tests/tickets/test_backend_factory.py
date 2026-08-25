import importlib


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


def test_sqlite_only_ignores_pg_url(monkeypatch):
    # SQLite-only build: a stray AIFORGE_PG_URL never flips the backend.
    envmod = _reload_env(monkeypatch, AIFORGE_PG_URL="postgresql://x/y")
    assert envmod.AIFORGE_USE_SQLITE is True
    assert envmod.AIFORGE_PG_URL is None


def test_sqlite_backend_satisfies_protocol():
    from aiforge_core.tickets.backends.base import StoreBackend
    from aiforge_core.tickets.backends.sqlite_backend import SqliteBackend
    proto = [m for m in dir(StoreBackend) if not m.startswith("_")]
    missing = [m for m in proto if not hasattr(SqliteBackend, m)]
    assert missing == [], f"SqliteBackend missing {missing}"
