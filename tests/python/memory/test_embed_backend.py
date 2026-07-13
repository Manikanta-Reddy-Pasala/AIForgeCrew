"""Embedder backend dispatch: model2vec (static, no torch) vs hash, and the
NO-FALLBACK contract (a vector backend selected + model missing → raises, not
silently hash)."""
from __future__ import annotations
import pytest


def test_hash_backend_default(monkeypatch):
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    from aiforge_core.memory import local_embed
    v = local_embed.embed("hello world")
    assert len(v) == local_embed.EMBED_DIM
    assert local_embed.embed_dim() == local_embed.EMBED_DIM


def test_model2vec_backend_dispatches(monkeypatch):
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "model2vec")
    import aiforge_core.integrations.model2vec_embed as m2
    monkeypatch.setattr(m2, "embed", lambda t: [0.1, 0.9])
    monkeypatch.setattr(m2, "dim", lambda: 2)
    from aiforge_core.memory import local_embed
    assert local_embed.embed("anything") == [0.1, 0.9]
    assert local_embed.embed_dim() == 2


def test_model2vec_no_silent_fallback(monkeypatch):
    """model2vec selected but the model can't load → RAISES (never hash)."""
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "model2vec")
    import aiforge_core.integrations.model2vec_embed as m2

    def _boom():
        raise ImportError("model2vec not installed")

    monkeypatch.setattr(m2, "_load", _boom)
    from aiforge_core.memory import local_embed
    with pytest.raises(Exception):
        local_embed.embed("x")


def test_vec_enabled_flag(monkeypatch):
    from aiforge_core.memory import sqlite_memory as sm
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    assert sm._vec_enabled() is False
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "model2vec")
    assert sm._vec_enabled() is True
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "api")
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
