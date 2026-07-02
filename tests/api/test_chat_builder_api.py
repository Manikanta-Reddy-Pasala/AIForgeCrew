"""Builder mode through the API: the /api/chat/agent endpoint threads the
`builder` field into run_chat_agent so the task charter reaches the system
prompt. LLM stubbed — no live endpoint."""
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
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_JOBS_DISABLE", "1")
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app), api


def _capture_llm(monkeypatch, final="FINAL: what should this job run, and how often?"):
    box: dict = {}

    def fake_complete(role, messages, **kw):
        box.setdefault("sys", messages[0]["content"])
        return final
    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)
    return box


def test_chat_agent_endpoint_injects_job_builder_charter(app_client, monkeypatch,
                                                         tmp_path):
    client, _ = app_client
    box = _capture_llm(monkeypatch)
    r = client.post("/api/chat/agent", json={
        "messages": [{"role": "user", "content": "pull my repos every morning"}],
        "cwd": str(tmp_path), "builder": "job"})
    assert r.status_code == 200
    _ = r.text                                  # drain the SSE stream
    assert "JOB-BUILDER MODE" in box["sys"]
    assert "create_job_script" in box["sys"]


def test_chat_agent_endpoint_without_builder_has_no_charter(app_client, monkeypatch,
                                                            tmp_path):
    client, _ = app_client
    box = _capture_llm(monkeypatch, final="FINAL: hi")
    r = client.post("/api/chat/agent", json={
        "messages": [{"role": "user", "content": "hello"}],
        "cwd": str(tmp_path)})
    assert r.status_code == 200
    _ = r.text
    assert "JOB-BUILDER MODE" not in box["sys"]
