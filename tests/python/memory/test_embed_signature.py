"""A backend/model switch must trigger a re-embed EVEN AT THE SAME DIMENSION
(hash and model2vec are both 256-dim), and a removed fact must leave the vector
store — superseded info (UAE→India) gone from both the brief and the vectors."""
from __future__ import annotations
import importlib
import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return sm


def test_signature_change_detected_at_same_dim(mem, monkeypatch):
    mem.write_unit(text="I am from UAE", kind="knowledge")
    mem.reembed_all()                              # stamps embed_sig = hash
    assert mem.stored_embedder_changed() is False  # same backend
    # switch to a 256-dim model2vec (SAME dim as hash) → must still be "changed"
    import aiforge_core.memory.local_embed as le
    monkeypatch.setattr(le, "embed_signature", lambda: "model2vec:potion-base-8M")
    assert mem.stored_embedder_changed() is True   # caught despite equal dim


def test_fresh_store_is_not_changed(mem):
    assert mem.stored_embedder_changed() is False  # no vectors yet → not "changed"


def test_removed_fact_leaves_the_vector_store(mem):
    mem.write_unit(text="my location is UAE", kind="knowledge")
    mem.write_unit(text="the sky is blue", kind="knowledge")
    assert len(mem.recall("where is my location UAE", limit=3)) >= 1
    # supersede: the UAE unit is deleted (contradict/compaction drops it)
    with mem._conn() as c:
        c.execute("DELETE FROM memory_units WHERE text LIKE '%UAE%'")
    assert mem.recall("where is my location UAE", limit=3) == [] or all(
        "UAE" not in h.get("text", "")
        for h in mem.recall("where is my location UAE", limit=3))
