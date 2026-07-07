"""End-to-end: a chat BUILDER run (the '/chat?builder=…' flow) creates a
skill / workflow / rule that shows up in the Library API — the real
click→artifact path, with the model stubbed. Guards the whole wire: builder
charter → finalize tool → correct store → Library list.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_JOBS_DISABLE", "1")
    monkeypatch.setenv("AIFORGE_CHAT_CTX_REPOMAP", "0")
    monkeypatch.setenv("AIFORGE_BUILDER_ELABORATE", "0")   # keep body verbatim
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app), str(tmp_path)


def _stub_two_turns(monkeypatch, action: str):
    calls = {"n": 0}

    def fake(role, messages, **kw):
        calls["n"] += 1
        return action if calls["n"] == 1 else "FINAL: done."
    monkeypatch.setattr("aiforge_core.llm.client.complete", fake)


@pytest.mark.parametrize("kind,action,needle", [
    ("skill",
     'ACTION: learn_skill {"name":"e2e-skill","description":"d","body":"step one",'
     '"triggers":["x"],"scope":"global"}',
     "e2e-skill"),
    ("workflow",
     'ACTION: learn_workflow {"name":"e2e-wf","description":"d","body":"1. do it",'
     '"triggers":["y"],"scope":"global"}',
     "e2e-wf"),
    ("rule",
     'ACTION: remember_rule {"text":"Always lint before commit","scope":"global"}',
     "always-lint-before-commit"),
])
def test_builder_creates_and_shows_in_library(app_client, monkeypatch,
                                              kind, action, needle):
    client, cwd = app_client
    _stub_two_turns(monkeypatch, action)
    r = client.post("/api/chat/agent", json={
        "messages": [{"role": "user", "content": f"make a {kind}"}],
        "cwd": cwd, "builder": kind})
    assert r.status_code == 200
    _ = r.text                                     # drain SSE stream
    lib = client.get(f"/api/library/{kind}s").json()
    names = [x["name"] for x in lib]
    assert needle in names, f"{kind} not in Library: {names}"
