"""Memory admin — cross-store overview + per-datasource destructive clear.

Covers the embedded (SQLite) default end-to-end plus a mocked-driver unit
test for the Neo4j graph clears (asserting the Cypher is SCOPED to the
AIForge-owned labels, never a blanket ``MATCH (n) DETACH DELETE n``), and
that a clear wipes DATA only — the registered memory sources survive.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


# ─────────────────────────── fixtures / helpers ─────────────────────────────

def _isolate_env(monkeypatch, tmp_path):
    """Point every embedded store at a throwaway tmp dir, force the SQLite
    memory backend, and strip any Neo4j/PG config so nothing external is hit."""
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_SOURCES_DB_PATH", str(tmp_path / "sources.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)


def _seed():
    """Put a unit in each embedded store so overview counts are non-zero."""
    from aiforge_core.memory import md_store, sqlite_memory
    from aiforge_core.runtime import chat_store, memory_sources
    sqlite_memory.write_unit(text="a green build recipe", kind="note")
    sqlite_memory.write_unit(text="a doer failure trace", kind="failure")
    # Write the md file straight to disk (md_store.write would ALSO ingest a
    # sqlite unit, which would throw off the exact sqlite counts below).
    (md_store.memory_dir() / "deploy-notes.md").write_text(
        "---\ntitle: Deploy notes\nkind: note\n---\nhow the NUC deploy works\n",
        encoding="utf-8")
    chat_store.reset_backend_for_tests()
    sid = chat_store.create_session("hello", role="chat")["id"]
    chat_store.add_message(sid, "user", "hi there")
    chat_store.add_message(sid, "assistant", "hello back")
    memory_sources.create("url", "https://example.com/x", "example")


def _reload_admin(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    import aiforge_core.memory.admin as admin
    importlib.reload(admin)
    from aiforge_core.runtime import chat_store
    chat_store.reset_backend_for_tests()
    return admin


def _fresh_api(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    monkeypatch.delenv("AIFORGE_BIND_HOST", raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    import aiforge_core.memory.admin as admin
    importlib.reload(admin)
    import aiforge_core.api.api as api
    importlib.reload(api)
    from aiforge_core.runtime import chat_store
    chat_store.reset_backend_for_tests()
    return api, admin


# ─────────────────────────────── overview ───────────────────────────────────

def test_overview_sqlite_backend_counts_each_store(monkeypatch, tmp_path):
    admin = _reload_admin(monkeypatch, tmp_path)
    _seed()

    ov = admin.memory_overview()
    assert ov["backend"] == "sqlite"
    stores = ov["stores"]

    # sqlite memory
    assert stores["sqlite"]["total"] == 2
    assert stores["sqlite"]["by_kind"].get("note") == 1
    assert stores["sqlite"]["by_kind"].get("failure") == 1

    # md files
    assert stores["md_files"]["count"] == 1
    assert stores["md_files"]["bytes"] > 0

    # chat
    assert stores["chat"]["sessions"] == 1
    assert stores["chat"]["messages"] == 2

    # sources (VIEW only)
    assert stores["sources"]["count"] == 1
    assert stores["sources"]["by_status"].get("idle") == 1
    assert len(stores["sources"]["items"]) == 1


def test_overview_graph_stores_unavailable_do_not_raise(monkeypatch, tmp_path):
    admin = _reload_admin(monkeypatch, tmp_path)
    ov = admin.memory_overview()
    for store in ("graph_facts", "symbols", "graphify", "chunks"):
        g = ov["stores"][store]
        assert g["available"] is False
        assert "reason" in g


# ──────────────────────────── per-store clear ───────────────────────────────

def test_clear_sqlite_empties_units(monkeypatch, tmp_path):
    admin = _reload_admin(monkeypatch, tmp_path)
    _seed()
    assert admin.memory_overview()["stores"]["sqlite"]["total"] == 2

    res = admin.clear_store("sqlite")
    assert res["ok"] is True
    assert res["deleted"] == 2
    assert admin.memory_overview()["stores"]["sqlite"]["total"] == 0
    # idempotent
    assert admin.clear_store("sqlite")["deleted"] == 0


def test_clear_chat_empties_sessions(monkeypatch, tmp_path):
    admin = _reload_admin(monkeypatch, tmp_path)
    _seed()
    res = admin.clear_store("chat")
    assert res["ok"] is True
    assert res["deleted"] == 1
    assert admin.memory_overview()["stores"]["chat"]["sessions"] == 0


def test_clear_md_files_removes_files(monkeypatch, tmp_path):
    admin = _reload_admin(monkeypatch, tmp_path)
    _seed()
    assert admin.memory_overview()["stores"]["md_files"]["count"] == 1
    res = admin.clear_store("md_files")
    assert res["ok"] is True
    assert res["deleted"] == 1
    assert admin.memory_overview()["stores"]["md_files"]["count"] == 0


def test_clear_unknown_store_raises(monkeypatch, tmp_path):
    admin = _reload_admin(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        admin.clear_store("nope")


def test_clear_result_notes_config_preserved(monkeypatch, tmp_path):
    admin = _reload_admin(monkeypatch, tmp_path)
    _seed()
    res = admin.clear_store("sqlite")
    assert "preserved" in res["note"]


def test_clear_all_wipes_data_but_keeps_sources(monkeypatch, tmp_path):
    admin = _reload_admin(monkeypatch, tmp_path)
    _seed()
    res = admin.clear_all()
    results = res["results"]
    # every clearable data store reported (sources is NOT clearable)
    for store in ("graph_facts", "symbols", "graphify", "chunks",
                  "sqlite", "md_files", "chat"):
        assert store in results
    assert "sources" not in results

    ov = admin.memory_overview()["stores"]
    assert ov["sqlite"]["total"] == 0
    assert ov["chat"]["sessions"] == 0
    assert ov["md_files"]["count"] == 0
    # CONFIG PRESERVED: the registered source survives the wipe.
    assert ov["sources"]["count"] == 1
    assert res["sources_reset"] == 1


# ─────────────────────── graph clears — mocked driver ───────────────────────

class _FakeCounters:
    nodes_deleted = 7


class _FakeResult:
    def consume(self):
        class _S:
            counters = _FakeCounters()
        return _S()

    def single(self):
        return {"n": 0}

    def __iter__(self):
        return iter([])


class _FakeSession:
    def __init__(self, captured):
        self._captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher, **params):
        self._captured.append((cypher, params))
        return _FakeResult()


class _FakeDriver:
    def __init__(self, captured):
        self._captured = captured

    def session(self):
        return _FakeSession(self._captured)

    def close(self):
        pass


def _run_graph_clear(admin, monkeypatch, store):
    captured: list = []
    monkeypatch.setattr(admin, "_graph_driver", lambda: _FakeDriver(captured))
    res = admin.clear_store(store)
    return res, captured


def test_clear_graph_facts_scoped_to_fact_labels(monkeypatch, tmp_path):
    admin = _reload_admin(monkeypatch, tmp_path)
    res, captured = _run_graph_clear(admin, monkeypatch, "graph_facts")
    assert res["ok"] is True
    assert res["deleted"] == 7

    cypher, params = captured[-1]
    assert "labels(n)" in cypher
    assert "$labels" in cypher
    normalized = " ".join(cypher.split()).upper()
    assert "MATCH (N) DETACH DELETE N" not in normalized  # never a blanket wipe
    for lbl in ("Observation_v2", "Decision_v2", "Fact", "MemoryBlock"):
        assert lbl in params["labels"]
    # facts clear must NOT touch code symbols/chunks
    assert "Symbol" not in params["labels"]
    assert "Chunk_v2" not in params["labels"]


def test_clear_symbols_scoped_to_symbol_labels(monkeypatch, tmp_path):
    admin = _reload_admin(monkeypatch, tmp_path)
    res, captured = _run_graph_clear(admin, monkeypatch, "symbols")
    assert res["ok"] is True
    cypher, params = captured[-1]
    assert "Symbol" in params["labels"]
    assert "Observation_v2" not in params["labels"]  # not the facts store


def test_clear_graphify_targets_graphify_nodes_only(monkeypatch, tmp_path):
    admin = _reload_admin(monkeypatch, tmp_path)
    res, captured = _run_graph_clear(admin, monkeypatch, "graphify")
    assert res["ok"] is True
    cypher, params = captured[-1]
    up = " ".join(cypher.split()).upper()
    assert "GRAPHIFYNODE" in up
    assert params.get("src") == "graphify"
    assert "MATCH (N) DETACH DELETE N" not in up


# ──────────────────────────── API endpoints ─────────────────────────────────

def test_api_overview_endpoint(monkeypatch, tmp_path):
    api, _admin = _fresh_api(monkeypatch, tmp_path)
    _seed()
    client = TestClient(api.app)
    r = client.get("/api/memory/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "sqlite"
    assert body["stores"]["sqlite"]["total"] == 2


def test_api_clear_requires_confirm(monkeypatch, tmp_path):
    api, admin = _fresh_api(monkeypatch, tmp_path)
    _seed()
    client = TestClient(api.app)

    # No confirm → 400, and nothing deleted.
    r = client.post("/api/memory/clear/sqlite")
    assert r.status_code == 400
    assert admin.memory_overview()["stores"]["sqlite"]["total"] == 2

    # confirm=false → 400 too.
    r = client.post("/api/memory/clear/sqlite", json={"confirm": False})
    assert r.status_code == 400

    # confirm=true → clears.
    r = client.post("/api/memory/clear/sqlite", json={"confirm": True})
    assert r.status_code == 200
    assert r.json()["deleted"] == 2
    assert admin.memory_overview()["stores"]["sqlite"]["total"] == 0


def test_api_clear_unknown_store_400(monkeypatch, tmp_path):
    api, _admin = _fresh_api(monkeypatch, tmp_path)
    client = TestClient(api.app)
    r = client.post("/api/memory/clear/bogus", json={"confirm": True})
    assert r.status_code == 400


def test_api_clear_all_requires_confirm(monkeypatch, tmp_path):
    api, admin = _fresh_api(monkeypatch, tmp_path)
    _seed()
    client = TestClient(api.app)

    assert client.post("/api/memory/clear-all").status_code == 400

    r = client.post("/api/memory/clear-all", json={"confirm": True})
    assert r.status_code == 200
    body = r.json()
    assert "results" in body
    assert admin.memory_overview()["stores"]["sqlite"]["total"] == 0
    # sources registration preserved across the full wipe
    assert admin.memory_overview()["stores"]["sources"]["count"] == 1
