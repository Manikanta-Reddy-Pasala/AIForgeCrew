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


def test_single_task_fallback_still_writes_spec(app_client, monkeypatch, tmp_path):
    """<2 subtasks → best-of-N/sequential fallback: SPEC.md must STILL land
    in the workspace (was only written by stream_parallel_team)."""
    import os
    client, api = app_client
    from aiforge_core.runtime import parallel_subtasks as pp
    monkeypatch.setenv("AIFORGE_BEST_OF_N", "2")
    monkeypatch.setattr(pp, "_enhance", lambda *a, **k: "ONE hard task spec")
    monkeypatch.setattr(pp, "_architect", lambda *a, **k: [])
    monkeypatch.setattr(pp, "_decompose", lambda *a, **k: [])
    monkeypatch.setattr(pp, "_is_greenfield", lambda *a, **k: True)
    from aiforge_core.runtime import best_of_n as bon
    monkeypatch.setattr(bon, "stream_best_of_n",
                        lambda *a, **k: iter([{"type": "message", "text": "ok"}]))
    sid = client.post("/api/chat/sessions",
                      json={"title": "t", "cwd": str(tmp_path)}).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/message", json={
        "content": "create a single-file parser tool with tests",
        "mode": "simple", "cwd": str(tmp_path)})
    assert r.status_code == 200
    spec = os.path.join(str(tmp_path), "SPEC.md")
    assert os.path.isfile(spec), "SPEC.md missing on the single-task fallback"
    assert "ONE hard task spec" in open(spec, encoding="utf-8").read()
    assert "Wrote SPEC.md" in r.text


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


def test_doc_word_mention_does_not_veto_a_real_build(app_client, monkeypatch, tmp_path):
    """Regression (live-caught): 'a monthly REPORT module' vetoed the build
    matcher via the document blocklist → 10-file build ran single-agent."""
    client, api = app_client
    from aiforge_core.runtime import parallel_subtasks as pp
    called = {"n": 0}
    monkeypatch.setattr(pp, "_enhance", lambda *a, **k: "SPEC")
    monkeypatch.setattr(pp, "_architect", lambda *a, **k: [
        {"path": "a.py", "purpose": "x", "api": []},
        {"path": "tests/test_a.py", "purpose": "t", "api": []}])
    monkeypatch.setattr(pp, "_is_greenfield", lambda *a, **k: True)

    def fake_team(spec, **kw):
        called["n"] += 1
        yield {"type": "message", "text": "pipeline ran"}

    monkeypatch.setattr(pp, "stream_parallel_team", fake_team)
    sid = client.post("/api/chat/sessions",
                      json={"title": "t", "cwd": str(tmp_path)}).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/message", json={
        "content": ("build an expense tracker cli tool in python: a storage "
                    "module using sqlite, a monthly report module, and the "
                    "cli entrypoint. include unit tests for every module"),
        "mode": "simple", "cwd": str(tmp_path)})
    assert r.status_code == 200 and called["n"] == 1
    # pure DOC tasks still stay off the pipeline
    assert "Multi-file build detected" in r.text


def test_pure_doc_task_still_vetoed(app_client, monkeypatch, tmp_path):
    client, api = app_client
    from aiforge_core.runtime import parallel_subtasks as pp

    def must_not_team(*a, **k):
        raise AssertionError("doc task escalated")

    monkeypatch.setattr(pp, "stream_parallel_team", must_not_team)
    monkeypatch.setattr("aiforge_core.runtime.chat_agent.run_chat_agent",
                        lambda *a, **k: iter([{"type": "message", "text": "ok"},
                                              {"type": "done"}]))
    sid = client.post("/api/chat/sessions",
                      json={"title": "t", "cwd": str(tmp_path)}).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/message", json={
        "content": "write a jira ticket describing the rate limiting work",
        "mode": "simple", "cwd": str(tmp_path)})
    assert r.status_code == 200 and "ok" in r.text
