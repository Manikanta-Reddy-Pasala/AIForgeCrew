"""The chat UI's request meter, over HTTP."""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app)


def test_llm_usage_reports_this_chats_requests(app_client):
    from aiforge_core.llm import call_meter
    call_meter.reset_all()
    sid = app_client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]

    r = app_client.get(f"/api/chat/sessions/{sid}/llm-usage")
    assert r.status_code == 200
    fresh = r.json()
    # A brand-new chat has fired nothing OF ITS OWN yet. `total` is
    # machine-wide, so a background probe on session-create may already have
    # bumped it — that is the point of keeping the two numbers apart.
    assert fresh["session_id"] == sid
    assert fresh["turn"] == 0 and fresh["session"] == 0 and fresh["by_role"] == {}
    base_total = fresh["total"]

    call_meter.turn_reset(sid)
    call_meter.record("doer", session_id=sid)
    call_meter.record("doer", session_id=sid)
    call_meter.record("learner")                     # another chat's / a fold's
    body = app_client.get(f"/api/chat/sessions/{sid}/llm-usage").json()
    assert body["turn"] == 2 and body["session"] == 2
    # `total` is machine-wide and other threads may add to it between the two
    # requests — assert it MOVED by at least our three, not that it is exact.
    assert body["total"] >= base_total + 3
    assert body["by_role"] == {"doer": 2}
    call_meter.reset_all()


def test_unknown_session_reports_zeros_not_a_leak(app_client):
    from aiforge_core.llm import call_meter
    call_meter.reset_all()
    body = app_client.get("/api/chat/sessions/987654321/llm-usage").json()
    assert body["turn"] == 0 and body["session"] == 0 and body["by_role"] == {}
