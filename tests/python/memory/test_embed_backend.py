"""Embedder backend dispatch: semantic (sentence-transformer) vs hash, and the
NO-FALLBACK contract (semantic selected + model missing → raises, not hash)."""
from __future__ import annotations
import pytest


def test_hash_backend_default(monkeypatch):
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    from aiforge_core.memory import local_embed
    v = local_embed.embed("hello world")
    assert len(v) == local_embed.EMBED_DIM
    assert local_embed.embed_dim() == local_embed.EMBED_DIM


def test_semantic_backend_dispatches(monkeypatch):
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "semantic")
    import aiforge_core.integrations.semantic_embed as se
    monkeypatch.setattr(se, "embed", lambda t: [0.1, 0.9])
    monkeypatch.setattr(se, "dim", lambda: 2)
    from aiforge_core.memory import local_embed
    assert local_embed.embed("anything") == [0.1, 0.9]
    assert local_embed.embed_dim() == 2


def test_semantic_no_silent_fallback(monkeypatch):
    """Semantic selected but the model can't load → RAISES (never hash)."""
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "semantic")
    import aiforge_core.integrations.semantic_embed as se

    def _boom():
        raise ImportError("sentence_transformers not installed")

    monkeypatch.setattr(se, "_load", _boom)
    from aiforge_core.memory import local_embed
    with pytest.raises(Exception):
        local_embed.embed("x")


def test_vec_enabled_flag(monkeypatch):
    from aiforge_core.memory import sqlite_memory as sm
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    assert sm._vec_enabled() is False
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "semantic")
    assert sm._vec_enabled() is True


def test_reembed_all_and_dim_match(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    import importlib
    import aiforge_core.memory.local_embed as le
    monkeypatch.setattr(le, "embed", lambda t: [0.1, 0.2, 0.3])
    monkeypatch.setattr(le, "embed_dim", lambda: 3)
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    sm.write_unit(text="fact one", kind="learning", repo="svc")
    sm.write_unit(text="fact two", kind="learning", repo="svc")
    assert sm.stored_dim_mismatch() is False        # stored (3) == active (3)
    assert sm.reembed_all()["reembedded"] == 2
    # a dim change is now detected
    monkeypatch.setattr(le, "embed_dim", lambda: 5)
    assert sm.stored_dim_mismatch() is True
