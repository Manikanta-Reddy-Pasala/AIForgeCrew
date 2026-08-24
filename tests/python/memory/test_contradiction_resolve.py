"""Cross-scope contradiction resolver: a new fact contradicting the repo OR the
global brief REPLACES the stale one (video's "overwrite outdated" rule)."""
from __future__ import annotations
import importlib
import types
import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return tmp_path


def test_contradiction_across_repo_and_global_is_replaced(mem, monkeypatch):
    from aiforge_core.memory import md_store as m
    # global brief says deploy via docker; repo now says systemctl (contradiction)
    m._brief_upsert("shared", "deploy via docker compose up")
    m._brief_upsert("svc", "deploy via systemctl restart the service")
    m._brief_upsert("svc", "OrderController maps POST /orders")   # unrelated, keep

    def _fake(role, messages, response_model, **kw):
        # the LLM flags the STALE global docker line to remove
        return types.SimpleNamespace(removes=[
            types.SimpleNamespace(scope="shared",
                                  fact="deploy via docker compose up")])
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)

    r = m.resolve_contradictions()
    assert r["removed"] == 1
    shared = m.read_file("compacted-shared")["body"]
    assert "docker compose up" not in shared              # stale global replaced
    svc = m.read_file("compacted-svc")["body"]
    assert "systemctl restart" in svc                     # current kept
    assert "OrderController" in svc                       # unrelated untouched


def test_no_contradiction_removes_nothing(mem, monkeypatch):
    from aiforge_core.memory import md_store as m
    m._brief_upsert("svc", "port 8091 for the sync service")
    m._brief_upsert("other", "port 8080 for the gateway")     # different subject

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete",
                        lambda *a, **k: types.SimpleNamespace(removes=[]))
    r = m.resolve_contradictions()
    assert r["removed"] == 0
    assert "8091" in m.read_file("compacted-svc")["body"]
    assert "8080" in m.read_file("compacted-other")["body"]


def test_disabled_via_env(mem, monkeypatch):
    from aiforge_core.memory import md_store as m
    monkeypatch.setenv("AIFORGE_OKR_CONTRADICT", "0")
    m._brief_upsert("svc", "a fact here for the svc")
    m._brief_upsert("shared", "another fact for everyone")
    assert m.resolve_contradictions() == {"removed": 0, "skipped": "disabled"}
