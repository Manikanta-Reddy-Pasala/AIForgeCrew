"""Adding a repo/docs memory source auto-starts the background index;
url/file stay manual."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_SOURCES_DB_PATH", str(tmp_path / "src.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_JOBS_DISABLE", "1")
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    import aiforge_core.runtime.memory_sources as ms
    importlib.reload(ms)
    import aiforge_core.runtime.memory_ingest as mi
    importlib.reload(mi)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app), api, mi


def test_repo_add_auto_indexes(app_client, monkeypatch, tmp_path):
    client, api, mi = app_client
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list = []
    # Indexing runs in a SEPARATE PROCESS (_spawn_index → subprocess.Popen) to
    # avoid GIL-starving uvicorn, so an in-process monkeypatch of run_index is
    # never seen by the child. Patch the _spawn_index seam the endpoint calls —
    # that's what proves the repo add auto-kicks the index.
    monkeypatch.setattr(api._r_memory, "_spawn_index", lambda sid: calls.append(sid))

    r = client.post("/api/memory/sources",
                    json={"kind": "repo", "location": str(repo), "name": "r"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "indexing"
    assert calls == [body["id"]]          # auto-index fired for the new source


def test_url_add_stays_manual(app_client, monkeypatch):
    client, api, mi = app_client
    calls: list = []
    monkeypatch.setattr(mi, "run_index", lambda sid: calls.append(sid))

    r = client.post("/api/memory/sources",
                    json={"kind": "url", "location": "https://example.com",
                          "name": "u"})
    assert r.status_code == 201
    assert r.json()["status"] == "idle"
    import time
    time.sleep(0.05)
    assert calls == []
