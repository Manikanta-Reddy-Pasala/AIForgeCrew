"""POST /api/chat/sessions/{id}/compact-context: the Compact button."""
import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_CHAT_COMPACT_WAIT_S", "0")
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app)


def _session(client):
    return client.post("/api/chat/sessions", json={}).json()["id"]


def test_running_turn_is_asked_to_compact(client, monkeypatch):
    from aiforge_core.runtime import chat_compact, chat_runs
    sid = _session(client)
    monkeypatch.setattr(chat_runs, "is_running", lambda s: True)
    r = client.post(f"/api/chat/sessions/{sid}/compact-context").json()
    assert r["mode"] == "running" and chat_compact.take(sid)


def test_idle_compacts_and_resumes_an_unfinished_task(client, monkeypatch):
    from aiforge_core.api.routes._chat import _message
    from aiforge_core.runtime import chat_compact
    sid = _session(client)
    monkeypatch.setattr(chat_compact, "compact_saved", lambda s, role="chat": {
        "ok": True, "compacted": True, "before": 100, "after": 10})
    monkeypatch.setattr(chat_compact, "last_turn_unfinished", lambda s: True)
    started = []
    monkeypatch.setattr(_message, "chat_session_message",
                        lambda s, body: started.append((s, body.content, body.resume)))
    r = client.post(f"/api/chat/sessions/{sid}/compact-context").json()
    assert r["mode"] == "idle" and r["compacted"] and r["resumed"]
    assert started == [(sid, "continue", True)]


def test_idle_finished_task_is_not_resumed(client, monkeypatch):
    from aiforge_core.runtime import chat_compact
    sid = _session(client)
    monkeypatch.setattr(chat_compact, "last_turn_unfinished", lambda s: False)
    r = client.post(f"/api/chat/sessions/{sid}/compact-context").json()
    assert r["mode"] == "idle" and not r.get("compacted") and "resumed" not in r


def test_unknown_session(client):
    assert client.post("/api/chat/sessions/99999/compact-context").status_code == 404
