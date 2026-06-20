"""Phase-1 smoke: the API boots and serves tickets on SQLite, no Postgres."""
import importlib

import pytest

from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    store.create(title="seed ticket", assignee_role="doer", project="demo")
    import aiforge_core.memory.backend_select as bsel
    importlib.reload(bsel)
    import aiforge_core.memory.sqlite_memory as sqlmem
    importlib.reload(sqlmem)
    sqlmem.write_unit(text="embedded memory seed about widgets", kind="learning",
                      source="learner", repo="demo")

    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app), store


def test_health_reports_sqlite_storage(client):
    c, _ = client
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["storage"] == "sqlite"
    assert body["ok"] is True


def test_list_tickets_on_sqlite(client):
    c, _ = client
    r = c.get("/api/tickets")
    assert r.status_code == 200
    rows = r.json()
    assert any(t["title"] == "seed ticket" for t in rows)
    # enrichment keys present
    assert "active_role" in rows[0]
    assert "started_at" in rows[0]


def test_get_ticket_detail_on_sqlite(client):
    c, store = client
    ident = store.create(title="detail me", assignee_role="doer",
                         project="demo").identifier
    r = c.get(f"/api/tickets/{ident}")
    assert r.status_code == 200
    body = r.json()
    assert body["ticket"]["identifier"] == ident
    assert body["events"] == [] or isinstance(body["events"], list)
    assert isinstance(body["children"], list)


def test_agents_endpoint_does_not_500_on_sqlite(client):
    c, _ = client
    r = c.get("/api/agents")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_memory_stats_embedded(client):
    c, _ = client
    r = c.get("/api/memory/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "sqlite"
    assert body["total"] >= 1


def test_memory_search_embedded(client):
    c, _ = client
    r = c.get("/api/memory/search", params={"q": "widgets"})
    assert r.status_code == 200
    rows = r.json()
    assert any("widget" in (row.get("text") or "") for row in rows)


def test_set_openai_compatible_role_with_key(client):
    c, _ = client
    r = c.put("/api/agents/v2/doer/config", json={
        "provider": "openai_compatible", "model": "qwen-coder",
        "base_url": "http://box:1234", "api_key": "sk-secret",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "openai_compatible"
    assert body["base_url"] == "http://box:1234"
    assert body["api_key_set"] is True
    # config GET reports key presence without echoing the secret
    cfg = c.get("/api/agents/v2/config").json()
    assert cfg["doer"]["api_key_set"] is True
    assert "sk-secret" not in str(cfg)


def test_chat_agent_sse_streams_events(client, monkeypatch):
    c, _ = client

    def _fake_agent(messages, *, cwd, role="doer", **kw):
        yield {"type": "thought", "text": "thinking"}
        yield {"type": "tool", "name": "list_dir", "args": {}, "result": {"ok": True}}
        yield {"type": "message", "text": "all done"}
        yield {"type": "done"}

    monkeypatch.setattr("aiforge_core.runtime.chat_agent.run_chat_agent",
                        _fake_agent)
    r = c.post("/api/chat/agent",
               json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    body = r.text
    assert '"type": "tool"' in body
    assert '"text": "all done"' in body
    assert '"type": "done"' in body


def test_providers_test_endpoint(client, monkeypatch):
    import aiforge_core.llm.providers.openai_compatible as oc
    monkeypatch.setattr(oc, "probe",
                        lambda base_url, api_key=None: {"ok": True,
                                                        "models": ["m1"]})
    c, _ = client
    r = c.post("/api/providers/test",
               json={"base_url": "http://box:1234", "api_key": ""})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "models": ["m1"]}


def test_chat_sessions_crud_and_message(client, monkeypatch):
    c, _ = client

    def _fake_agent(messages, *, cwd, role="doer", **kw):
        yield {"type": "tool", "name": "noop", "args": {}, "result": {"ok": True}}
        yield {"type": "message", "text": f"turns={len(messages)}"}
        yield {"type": "done"}

    monkeypatch.setattr("aiforge_core.runtime.chat_agent.run_chat_agent",
                        _fake_agent)

    s = c.post("/api/chat/sessions", json={}).json()
    sid = s["id"]
    assert s["title"] == "New chat"

    r = c.post(f"/api/chat/sessions/{sid}/message",
               json={"content": "fix the parser bug"})
    assert r.status_code == 200
    assert "turns=1" in r.text
    assert '"type": "tool"' in r.text

    got = c.get(f"/api/chat/sessions/{sid}").json()
    assert got["session"]["title"] == "fix the parser bug"
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    assert got["messages"][1]["content"] == "turns=1"
    assert got["messages"][1]["steps"]

    r2 = c.post(f"/api/chat/sessions/{sid}/message",
                json={"content": "now add a test"})
    assert "turns=3" in r2.text

    assert any(x["id"] == sid for x in c.get("/api/chat/sessions").json())
    assert c.patch(f"/api/chat/sessions/{sid}",
                   json={"title": "Parser work"}).json()["title"] == "Parser work"
    assert c.delete(f"/api/chat/sessions/{sid}").status_code == 204
    assert c.get(f"/api/chat/sessions/{sid}").status_code == 404


def test_chat_models_and_session_role(client, monkeypatch):
    c, _ = client

    def _fake_agent(messages, *, cwd, role="doer", **kw):
        yield {"type": "message", "text": f"role={role}"}
        yield {"type": "done"}

    monkeypatch.setattr("aiforge_core.runtime.chat_agent.run_chat_agent",
                        _fake_agent)

    models = c.get("/api/chat/models").json()
    assert isinstance(models, list) and models
    assert all("role" in m and "model" in m for m in models)

    # create with a chosen role
    s = c.post("/api/chat/sessions", json={"role": "planner"}).json()
    assert s["role"] == "planner"
    sid = s["id"]
    r = c.post(f"/api/chat/sessions/{sid}/message", json={"content": "hi"})
    assert "role=planner" in r.text

    # per-message override sticks on the session
    r2 = c.post(f"/api/chat/sessions/{sid}/message",
                json={"content": "switch", "role": "doer"})
    assert "role=doer" in r2.text
    assert c.get(f"/api/chat/sessions/{sid}").json()["session"]["role"] == "doer"


def test_memory_stats_neo4j_routing(client, monkeypatch):
    c, _ = client
    import aiforge_core.api.api as api
    monkeypatch.setattr("aiforge_core.memory.backend_select.memory_backend",
                        lambda: "neo4j")
    monkeypatch.setattr(api, "_neo4j_stats",
                        lambda: {"backend": "neo4j", "total": 7, "wings": []})
    r = c.get("/api/memory/stats")
    assert r.status_code == 200
    assert r.json() == {"backend": "neo4j", "total": 7, "wings": []}
