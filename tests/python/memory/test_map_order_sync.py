"""map_scopes embedding-order batching + md->vector delete sync."""
from __future__ import annotations
import importlib
import pytest


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return tmp_path


def test_order_by_similarity_groups_similar(monkeypatch, mem):
    from aiforge_core.memory import md_store
    import aiforge_core.memory.local_embed as le
    # aX vectors near [1,0], bX near [0,1] — interleaved input
    vmap = {"a1": [1, 0], "b1": [0, 1], "a2": [0.9, 0.1], "b2": [0.1, 0.9]}
    monkeypatch.setattr(le, "embed", lambda t: vmap.get(t.split(":")[0], [0, 0]))
    briefs = [{"key": k, "summary": k} for k in ["a1", "b1", "a2", "b2"]]
    order = [b["key"] for b in md_store._order_briefs_by_similarity(briefs)]
    # a's adjacent, b's adjacent (not interleaved)
    assert abs(order.index("a1") - order.index("a2")) == 1
    assert abs(order.index("b1") - order.index("b2")) == 1


def test_prune_missing_file_rows(mem):
    from aiforge_core.memory import sqlite_memory as sm
    sm.write_unit(text="live", kind="learning", repo="svc", source="md:live")
    sm.write_unit(text="gone", kind="learning", repo="svc", source="md:gone")
    sm.write_unit(text="chat", kind="chat_summary", repo="svc",
                  source="chat-session:1")
    n = sm.prune_missing_file_rows({"md:live"})
    assert n == 1   # md:gone pruned; chat-session: untouched
    assert sm.recall("chat", repo="svc", limit=5)  # chat row survives


def test_delete_file_removes_index_row(mem):
    from aiforge_core.memory import md_store, sqlite_memory as sm
    md_store.write("note one", "some body text", kind="note", repo="svc")
    stem = [p.stem for p in md_store.memory_dir().glob("*.md")][0]
    assert sm.recall("body text", repo="svc", limit=5)
    md_store.delete_file(stem)
    assert not any("body text" in (h.get("text") or "")
                   for h in sm.recall("body text", repo="svc", limit=5))
