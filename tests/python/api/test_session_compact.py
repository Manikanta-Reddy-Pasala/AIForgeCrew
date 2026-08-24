"""T4 — POST /api/chat/sessions/{id}/compact route wiring."""
from __future__ import annotations

import importlib
import types

import pytest
from fastapi.testclient import TestClient


def _fresh_app(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    monkeypatch.delenv("AIFORGE_BIND_HOST", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    from aiforge_core.runtime import chat_store
    chat_store.reset_backend_for_tests()
    return api


def test_compact_missing_session_404(monkeypatch, tmp_path):
    api = _fresh_app(monkeypatch, tmp_path)
    client = TestClient(api.app)
    assert client.post("/api/chat/sessions/9999/compact").status_code == 404


def test_compact_session_captures(monkeypatch, tmp_path):
    api = _fresh_app(monkeypatch, tmp_path)
    from aiforge_core.runtime import chat_store
    sid = chat_store.create_session("hello", role="chat")["id"]
    chat_store.add_message(sid, "user", "always run tests before commit")
    chat_store.add_message(sid, "assistant", "noted")

    def _fake(role, messages, model, *a, **k):
        n = getattr(model, "__name__", "")
        if n == "ScopeDecision":
            return types.SimpleNamespace(scope="global", repo="", topic="")
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(text="always run tests before commit",
                                  kind="learning")])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    client = TestClient(api.app)
    r = client.post(f"/api/chat/sessions/{sid}/compact")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"]
    assert body["captured"] == 1
