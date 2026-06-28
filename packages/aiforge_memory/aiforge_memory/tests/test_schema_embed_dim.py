"""N11 — vector index dimension follows the embed config, not a
hardcoded 1024."""
from __future__ import annotations

from aiforge_memory.core import neo4j as schema


def test_default_dim_is_1024(monkeypatch):
    monkeypatch.delenv("AIFORGE_EMBED_DIM", raising=False)
    assert schema._embed_dim() == 1024


def test_dim_follows_embed_env(monkeypatch):
    monkeypatch.setenv("AIFORGE_EMBED_DIM", "768")
    assert schema._embed_dim() == 768
    stmts = schema._vector_index_statements(schema._embed_dim())
    assert len(stmts) == 1  # observation index only — chunk vectors removed
    for stmt in stmts:
        assert "`vector.dimensions`: 768" in stmt
        assert "1024" not in stmt


def test_static_statements_carry_no_hardcoded_vector_dim():
    joined = " ".join(schema._INDEX_STATEMENTS)
    assert "vector.dimensions" not in joined
    assert "CREATE VECTOR INDEX" not in joined
