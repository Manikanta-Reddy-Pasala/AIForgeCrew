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
    # GET — items carry applied_flags
    listing = client.get("/api/rules").json()
    item = next(i for i in listing["items"] if i["id"] == rid)
    assert "applied_flags" in item
    assert "global" in listing["by_scope"]
    # PUT scope (global → project — both persistent, so undo can reverse it)
    pr = client.put(f"/api/rules/{rid}/scope", json={"scope": "project"})
    assert pr.status_code == 200 and pr.json()["scope"] == "project"
    # DELETE
    dr = client.delete(f"/api/rules/{rid}")
    assert dr.status_code == 200 and dr.json()["ok"] is True


def test_capture_offers_gate_intent_without_setting_flag(app_client, monkeypatch):
    """A 'commit directly' rule yields a captured event with gate_intent but
    sets NO flag (the user must opt in explicitly)."""
    client, api = app_client
    from aiforge_core.runtime import rule_capture
    monkeypatch.setattr(rule_capture, "classify", lambda *a, **k: {
        "category": "rule", "scope": "session",
        "canonical": "commit directly, the machine has access",
        "confidence": 0.95, "task_present": False})
    s = client.post("/api/chat/sessions", json={}).json()
    r = client.post(f"/api/chat/sessions/{s['id']}/message",
                    json={"content": "from now on commit directly, machine has access",
                          "mode": "simple"})
    evs = _events(r.text)
    cap = [e for e in evs if e.get("type") == "captured"]
    assert cap and cap[0].get("gate_intent") == "commit"
    # No flag set by capture — the gate-disable list is empty
    flags = client.get("/api/rules/flags").json()["by_scope"]
    assert not flags.get("session")


def test_gate_flags_endpoints_set_list_revoke(app_client):
    client, api = app_client
    # global refused without confirm
    r = client.post("/api/rules/flags", json={
        "name": "commit_auto_approve", "scope": "global"})
    assert r.json()["applied"] is False
    # session opt-in honored
    r2 = client.post("/api/rules/flags", json={
        "name": "commit_auto_approve", "scope": "session", "session_id": 7})
    assert r2.json()["applied"] is True
    listed = client.get("/api/rules/flags").json()["by_scope"]
    assert "7" in listed["session"]
    # revoke
    d = client.delete("/api/rules/flags/commit_auto_approve",
                      params={"scope": "session", "session_id": 7})
    assert d.json()["ok"] is True
    assert not client.get("/api/rules/flags").json()["by_scope"].get("session")


def test_prefilter_skips_classify_for_trivial(app_client, monkeypatch):
    """A no-cue message ('fix the bug') never calls the LLM classifier."""
    client, api = app_client
    from aiforge_core.llm import client as llm_client
    from aiforge_core.runtime import chat_agent, chat_title, parallel_subtasks as pp
    called = {"n": 0}

    def _complete(*a, **k):
        called["n"] += 1
        return "{}"
    monkeypatch.setattr(llm_client, "complete", _complete)
    # Model-titling also calls the LLM on a fresh session — not the classifier;
    # stub it so this test isolates the rule-capture prefilter.
    monkeypatch.setattr(chat_title, "suggest_title", lambda *a, **k: "")
    monkeypatch.setattr(pp, "_enhance", lambda *a, **k: "spec")
    monkeypatch.setattr(chat_agent, "run_chat_agent",
                        lambda *a, **k: iter([{"type": "message", "text": "ok"},
                                              {"type": "done"}]))
    s = client.post("/api/chat/sessions", json={}).json()
    r = client.post(f"/api/chat/sessions/{s['id']}/message",
                    json={"content": "fix the bug", "mode": "simple"})
    assert r.status_code == 200
    # classify (the only place rule_capture calls the LLM) was never invoked
    assert called["n"] == 0
    assert not any(e.get("type") == "captured" for e in _events(r.text))


def test_short_circuit_backstop_forces_agent_run(app_client, monkeypatch):
    """'always commit directly, and now fix the bug' must RUN the agent even
    though the classifier reports task_present=false."""
    client, api = app_client
    from aiforge_core.runtime import chat_agent, parallel_subtasks as pp
    from aiforge_core.runtime import rule_capture
    monkeypatch.setattr(rule_capture, "classify", lambda *a, **k: {
        "category": "rule", "scope": "session", "canonical": "commit directly",
        "confidence": 0.95, "task_present": False})
    ran = {"n": 0}

    def _fake_run(*a, **k):
        ran["n"] += 1
        return iter([{"type": "message", "text": "did it"}, {"type": "done"}])
    monkeypatch.setattr(pp, "_enhance", lambda *a, **k: "spec")
    monkeypatch.setattr(chat_agent, "run_chat_agent", _fake_run)
    s = client.post("/api/chat/sessions", json={}).json()
    r = client.post(f"/api/chat/sessions/{s['id']}/message",
                    json={"content": "always commit directly, and now fix the bug",
                          "mode": "simple"})
    assert r.status_code == 200
    assert ran["n"] == 1  # agent ran — the real task was not dropped


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
