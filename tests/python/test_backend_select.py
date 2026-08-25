import importlib


def _reload(monkeypatch, **env):
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI",
              "AIFORGE_PG_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import aiforge_core.memory.backend_select as bs
    return importlib.reload(bs)


def test_default_is_sqlite(monkeypatch):
    bs = _reload(monkeypatch)
    assert bs.memory_backend() == "sqlite"
    assert bs.embedded() is True


def test_sqlite_only_ignores_legacy_backend_env(monkeypatch):
    # SQLite-only build: the legacy Postgres/Neo4j backend env vars no longer
    # switch the memory backend — it is always embedded SQLite.
    bs = _reload(monkeypatch, AIFORGE_PG_URL="postgresql://x/y",
                 NEO4J_URI="bolt://host:7687")
    assert bs.memory_backend() == "sqlite"
    assert bs.embedded() is True


def test_explicit_override_is_ignored(monkeypatch):
    bs = _reload(monkeypatch, AIFORGE_MEMORY_BACKEND="postgres")
    assert bs.memory_backend() == "sqlite"
    assert bs.embedded() is True
