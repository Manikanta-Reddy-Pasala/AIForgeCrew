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


def test_neo4j_env_selects_neo4j(monkeypatch):
    bs = _reload(monkeypatch, NEO4J_URI="bolt://host:7687")
    assert bs.memory_backend() == "neo4j"
    assert bs.embedded() is False


def test_aiforge_neo4j_env_selects_neo4j(monkeypatch):
    bs = _reload(monkeypatch, AIFORGE_NEO4J_URI="bolt://host:7687")
    assert bs.memory_backend() == "neo4j"


def test_pg_env_selects_postgres(monkeypatch):
    bs = _reload(monkeypatch, AIFORGE_PG_URL="postgresql://x/y")
    assert bs.memory_backend() == "postgres"


def test_explicit_override_wins(monkeypatch):
    bs = _reload(monkeypatch, AIFORGE_MEMORY_BACKEND="sqlite",
                 NEO4J_URI="bolt://host:7687")
    assert bs.memory_backend() == "sqlite"


def test_explicit_neo4j_without_env(monkeypatch):
    bs = _reload(monkeypatch, AIFORGE_MEMORY_BACKEND="neo4j")
    assert bs.memory_backend() == "neo4j"
