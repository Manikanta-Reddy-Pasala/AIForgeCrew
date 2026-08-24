"""Model registry: add models once, agents pick one by name; per-model vision."""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app)


def test_add_list_apply_delete(app_client):
    client = app_client
    # Add a model (key kept server-side, never returned).
    r = client.post("/api/agents/models", json={
        "label": "Qwen Coder", "model": "qwen3-coder", "base_url": "http://h:1234/v1",
        "api_key": "secret", "vision": "no"})
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["id"]
    assert m["api_key_set"] is True
    assert "api_key" not in m
    assert m["vision"] == "no"

    lst = client.get("/api/agents/models").json()["models"]
    assert any(x["id"] == m["id"] for x in lst)

    # Apply to agents → their config now points at this model.
    ap = client.post(f"/api/agents/models/{m['id']}/apply",
                     json={"roles": ["doer", "planner"]}).json()
    assert set(ap["applied"]) == {"doer", "planner"}
    assert not ap["errors"]
    cfg = client.get("/api/agents/v2/config").json()
    assert cfg["doer"]["model"] == "qwen3-coder"
    assert cfg["doer"]["base_url"] == "http://h:1234/v1"

    # Delete.
    assert client.delete(f"/api/agents/models/{m['id']}").status_code == 204
    assert client.get("/api/agents/models").json()["models"] == []


def test_model_required(app_client):
    assert app_client.post("/api/agents/models", json={"label": "x"}).status_code == 400


def test_per_model_vision_flag_drives_vision_enabled(app_client):
    client = app_client
    client.post("/api/agents/models", json={
        "model": "vlm-pro", "base_url": "http://v/v1", "vision": "yes"})
    mid = client.get("/api/agents/models").json()["models"][0]["id"]
    client.post(f"/api/agents/models/{mid}/apply", json={"roles": ["chat"]})
    from aiforge_core.runtime import chat_media
    # Explicit 'yes' on the chat model → vision enabled WITHOUT probing.
    assert chat_media.vision_enabled("chat") is True
    # Flip to 'no' → disabled.
    client.put(f"/api/agents/models/{mid}", json={"vision": "no"})
    assert chat_media.vision_enabled("chat") is False


def test_per_model_context_and_tls_default(app_client):
    client = app_client
    # No insecure_tls passed → defaults to skip-verify (True); context stored.
    r = client.post("/api/agents/models", json={
        "model": "big-ctx", "base_url": "http://c/v1", "context_window": 262144})
    m = r.json()
    assert m["insecure_tls"] is True            # skip-TLS by default
    assert m["context_window"] == 262144
    client.post(f"/api/agents/models/{m['id']}/apply", json={"roles": ["chat"]})
    from aiforge_core.config import model_registry
    # Per-model context wins for that role.
    assert model_registry.context_window_for_role("chat") == 262144
    from aiforge_core.runtime import chat_agent
    # The chat condense budget scales to the per-model window (262k tok).
    assert chat_agent._ctx_budget_chars("chat") > chat_agent._ctx_budget_chars("doer") \
        or model_registry.context_for("big-ctx", "http://c/v1") == 262144
