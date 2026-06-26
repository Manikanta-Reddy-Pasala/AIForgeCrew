"""API surface for Rule/Memory/Feedback capture: the pre-agent `captured` SSE
event + pure-capture short-circuit, the /api/rules endpoints, and fail-open
(a raising classify must not break the chat turn)."""
import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI", "AIFORGE_RULE_CAPTURE_DISABLE"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    from aiforge_core.runtime import rule_capture
    importlib.reload(rule_capture)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app), api


def _events(text: str):
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except ValueError:
                pass
    return out


def test_pure_capture_short_circuits_with_event(app_client, monkeypatch):
    client, api = app_client
    from aiforge_core.runtime import rule_capture
    monkeypatch.setattr(rule_capture, "classify", lambda *a, **k: {
        "category": "rule", "scope": "global",
        "canonical": "always use yarn", "confidence": 0.95,
        "task_present": False})
    s = client.post("/api/chat/sessions", json={}).json()
    r = client.post(f"/api/chat/sessions/{s['id']}/message",
                    json={"content": "from now on always use yarn", "mode": "simple"})
    assert r.status_code == 200
    evs = _events(r.text)
    cap = [e for e in evs if e.get("type") == "captured"]
    assert cap and cap[0]["category"] == "rule" and cap[0]["scope"] == "global"
    # pure capture → brief ack message, no enhancer/agent run
    msgs = [e for e in evs if e.get("type") == "message"]
    assert msgs and "saved" in msgs[0]["text"].lower()
    assert not any(e.get("type") == "thought" and e.get("role") == "enhancer"
                   for e in evs)
    assert any(e.get("type") == "done" for e in evs)


def test_rules_endpoints_list_rescope_delete(app_client):
    client, api = app_client
    from aiforge_core.runtime import rule_capture
    out = rule_capture.store({"category": "rule", "scope": "global",
                              "canonical": "always lint", "confidence": 0.9})
    rid = out["id"]
    # GET
    listing = client.get("/api/rules").json()
    assert any(i["id"] == rid for i in listing["items"])
    assert "global" in listing["by_scope"]
    # PUT scope (global → project — both persistent, so undo can reverse it)
    pr = client.put(f"/api/rules/{rid}/scope", json={"scope": "project"})
    assert pr.status_code == 200 and pr.json()["scope"] == "project"
    # DELETE
    dr = client.delete(f"/api/rules/{rid}")
    assert dr.status_code == 200 and dr.json()["ok"] is True


def test_capture_failure_is_fail_open(app_client, monkeypatch):
    client, api = app_client
    from aiforge_core.runtime import rule_capture
    from aiforge_core.runtime import parallel_subtasks as pp

    def boom(*a, **k):
        raise RuntimeError("classify exploded")
    monkeypatch.setattr(rule_capture, "classify", boom)
    # neuter the real agent run so the turn finishes cheaply (run_chat_agent is
    # imported inside the handler, so patch it on its defining module)
    from aiforge_core.runtime import chat_agent
    monkeypatch.setattr(pp, "_enhance", lambda *a, **k: "spec")
    monkeypatch.setattr(chat_agent, "run_chat_agent",
                        lambda *a, **k: iter([{"type": "message", "text": "ok"},
                                              {"type": "done"}]))
    s = client.post("/api/chat/sessions", json={}).json()
    r = client.post(f"/api/chat/sessions/{s['id']}/message",
                    json={"content": "build me a thing", "mode": "simple"})
    assert r.status_code == 200
    evs = _events(r.text)
    # no captured event, the turn still completed normally
    assert not any(e.get("type") == "captured" for e in evs)
    assert any(e.get("type") == "done" for e in evs)
    assert not any(e.get("type") == "error" for e in evs)
