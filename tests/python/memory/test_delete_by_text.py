"""W4 — scope-scoped delete of a moved fact's stale index row."""
from __future__ import annotations
import importlib
import pytest


@pytest.fixture()
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import aiforge_core.memory.local_embed as le
    monkeypatch.setattr(le, "embed", lambda t: [1.0, 0.0])
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return sm


def test_delete_scoped_to_repo(sm):
    sm.write_unit(text="OrderController maps /orders", kind="learning", repo="svc")
    sm.write_unit(text="OrderController maps /orders", kind="learning", repo="other")
    n = sm.delete_by_text_contains("OrderController maps /orders", repo="svc")
    assert n == 1
    rest = sm.recall("OrderController", repo="other", limit=10)
    assert any("OrderController" in h.get("text", "") for h in rest)  # other kept


def test_delete_requires_repo(sm):
    sm.write_unit(text="x", kind="learning", repo="svc")
    assert sm.delete_by_text_contains("x", repo="") == 0


def test_delete_excludes_compacted_brief_row(sm):
    # the consolidated brief row contains every fact; must NOT be deleted
    sm.write_unit(text="svc brief\n\n- OrderController maps /orders\n- more",
                  kind="knowledge", repo="svc")
    sm.write_unit(text="OrderController maps /orders", kind="learning", repo="svc")
    n = sm.delete_by_text_contains("OrderController maps /orders", repo="svc",
                                   exclude_kind="knowledge")
    assert n == 1  # only the per-capture row, not the brief
    hits = sm.recall("OrderController", repo="svc", limit=10)
    assert any(h.get("kind") == "knowledge" for h in hits)  # brief survived
