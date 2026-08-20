"""Agent limits over HTTP: 0 must be SAVEABLE, not just honoured.

The store's bounds and the loop's clamp both accepted 0 while the route's
pydantic body still said ``ge=1`` — so the Settings card's "No limits" button
422'd, nothing was written (not even the sibling field in the same body), and
every store-level test stayed green because it bypassed the route.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI",
              "AIFORGE_CHAT_SAFETY_CAP", "AIFORGE_CHAT_TURN_DEADLINE_S"):
        monkeypatch.delenv(k, raising=False)
    from aiforge_core.config import _filecache
    _filecache.clear()
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app)


def test_no_limits_saves_both_guards_and_the_runtime_agrees(client):
    r = client.put("/api/runtime/llm-settings",
                   json={"chat_safety_cap": 0, "chat_turn_deadline_s": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chat_safety_cap"] == 0
    assert body["chat_turn_deadline_s"] == 0
    # And the value the CHAT LOOP reads is the one that was saved.
    from aiforge_core.runtime.chat_agent._context import _limits
    assert _limits._safety_cap() == 0
    assert _limits._turn_deadline_s() == 0.0


def test_reset_brings_the_guards_back(client):
    assert client.put("/api/runtime/llm-settings",
                      json={"chat_safety_cap": 0}).status_code == 200
    r = client.put("/api/runtime/llm-settings",
                   json={"unset": ["chat_safety_cap", "chat_turn_deadline_s",
                                   "chat_cap_extensions"]})
    assert r.status_code == 200
    assert r.json()["chat_safety_cap"] == 2000


def test_a_negative_cap_is_still_refused(client):
    r = client.put("/api/runtime/llm-settings", json={"chat_safety_cap": -1})
    assert r.status_code == 422
