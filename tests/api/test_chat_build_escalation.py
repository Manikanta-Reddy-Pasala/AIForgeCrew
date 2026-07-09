"""SIMPLE mode must ESCALATE a multi-file build to the pipeline (complexity
routing) — regression for 'simple chat goes straight into implementation'.
With AIFORGE_PARALLEL_SUBTASKS defaulting ON, a build-shaped prompt in mode
'simple' routes through enhance→architect→subtasks, NOT the single agent."""
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
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    for k in ("AIFORGE_PARALLEL_SUBTASKS", "AIFORGE_AUTO_ESCALATE"):
        monkeypatch.delenv(k, raising=False)   # exercise the DEFAULTS
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app), api


def test_simple_mode_escalates_multifile_build(app_client, monkeypatch, tmp_path):
    client, api = app_client
    from aiforge_core.runtime import parallel_subtasks as pp
    called = {"team": 0}
    monkeypatch.setattr(pp, "_enhance", lambda *a, **k: "SPEC")
    monkeypatch.setattr(pp, "_architect", lambda *a, **k: [
        {"path": "a.java", "purpose": "x", "api": []},
        {"path": "b.java", "purpose": "y", "api": []}])
    monkeypatch.setattr(pp, "_is_greenfield", lambda *a, **k: True)

    def fake_team(spec, **kw):
        called["team"] += 1
        yield {"type": "message", "text": "pipeline ran"}

    monkeypatch.setattr(pp, "stream_parallel_team", fake_team)

    def must_not_run(*a, **k):
        raise AssertionError("single agent ran — escalation did not fire")

    monkeypatch.setattr("aiforge_core.runtime.chat_agent.run_chat_agent",
                        must_not_run)

    sid = client.post("/api/chat/sessions",
                      json={"title": "t", "cwd": str(tmp_path)}).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/message", json={
        "content": "create a simple poc on spring boot having 10 files with test cases",
        "mode": "simple", "cwd": str(tmp_path)})
    assert r.status_code == 200
    body = r.text
    assert called["team"] == 1, "pipeline was not invoked"
    assert "Multi-file build detected" in body


def test_simple_mode_question_stays_single_agent(app_client, monkeypatch, tmp_path):
    client, api = app_client
    from aiforge_core.runtime import parallel_subtasks as pp

    def must_not_team(*a, **k):
        raise AssertionError("advice question escalated to pipeline")

    monkeypatch.setattr(pp, "stream_parallel_team", must_not_team)
    monkeypatch.setattr("aiforge_core.runtime.chat_agent.run_chat_agent",
                        lambda *a, **k: iter([{"type": "message", "text": "answer"},
                                              {"type": "done"}]))
    sid = client.post("/api/chat/sessions",
                      json={"title": "t", "cwd": str(tmp_path)}).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/message", json={
        "content": "how do I build a spring boot app with tests?",
        "mode": "simple", "cwd": str(tmp_path)})
    assert r.status_code == 200 and "answer" in r.text
