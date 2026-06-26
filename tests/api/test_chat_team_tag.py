"""item 7 — a parallel / best-of-N chat run is persisted as team=True.

Before the fix the persist call hard-coded ``team=False``, mis-tagging the
auto-memory note for every parallel / best-of-N run. The endpoint now passes
``team=(team or _path["parallel"])``.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)
    # Enable the parallel team path + best-of-N route.
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS", "1")
    monkeypatch.setenv("AIFORGE_BEST_OF_N", "2")
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app), api


def test_parallel_run_persisted_as_team(app_client, monkeypatch):
    client, api = app_client

    # Force the orchestrator down the best-of-N route (no ≥2 distinct files),
    # and make the runners hermetic + instant.
    from aiforge_core.runtime import best_of_n as bon
    from aiforge_core.runtime import parallel_subtasks as pp
    monkeypatch.setattr(pp, "_enhance", lambda *a, **k: "spec")
    monkeypatch.setattr(pp, "_architect", lambda *a, **k: [])
    monkeypatch.setattr(pp, "_plan_files", lambda *a, **k: [])
    monkeypatch.setattr(pp, "_decompose", lambda *a, **k: [])   # <2 → best-of-N
    monkeypatch.setattr(
        bon, "stream_best_of_n",
        lambda *a, **k: iter([{"type": "message", "text": "done"},
                              {"type": "done"}]))

    captured: dict = {}
    from aiforge_core.runtime import chat_persist
    monkeypatch.setattr(chat_persist, "persist_turn",
                        lambda **kw: captured.update(kw))

    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/message",
                    json={"content": "build one hard thing", "mode": "team"})
    _ = r.text                       # drain the SSE stream → run the finally block

    assert captured.get("team") is True
