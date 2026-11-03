"""The chat's model picker while a turn runs (llm/model_switch): the pick is
saved as usual, and the answer says whether the step in flight is retried on
the new model or the next step simply uses it. Plus the failover settings
routes (config/chat_failover)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiforge_core.api.routes import chat as ch
from aiforge_core.api.routes._chat import _models
from aiforge_core.llm import model_switch


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "get_session", lambda sid: {"id": sid})
    monkeypatch.setattr(ch._acfg, "archetypes", lambda: ["chat"])
    monkeypatch.setattr(ch._acfg, "get", lambda role: {"model": "model-a",
                                                       "provider": "openai_compatible"})
    monkeypatch.setattr(_models, "_endpoint_for_picked_model",
                        lambda m, cur, url="": (None, None, False))
    monkeypatch.setattr(_models, "_served_model_ids_for_role", lambda role: set())
    model_switch.forget()
    app = FastAPI()
    app.include_router(ch.router)
    yield TestClient(app)
    model_switch.forget()


def _picks(monkeypatch, *models):
    seq = iter(models)
    monkeypatch.setattr(_models, "_effective_chat_model", lambda sid: next(seq))


def test_a_pick_mid_run_with_a_request_in_flight_says_retrying(client, monkeypatch):
    from aiforge_core.runtime import chat_cancel
    chat_cancel.start(31)
    model_switch.enter(31)                       # a request waiting on model-a
    try:
        _picks(monkeypatch, ("model-a", ""), ("model-z", ""))
        r = client.put("/api/chat/sessions/31/model", json={"model": "model-z"})
        assert r.status_code == 200
        assert r.json()["switch"] == "retrying"
        sw = model_switch.latest(31)
        assert (sw["from"], sw["to"]) == ("model-a", "model-z")
    finally:
        model_switch.leave(31)
        chat_cancel.finish(31)


def test_a_pick_mid_run_between_steps_says_next_step(client, monkeypatch):
    from aiforge_core.runtime import chat_cancel
    chat_cancel.start(32)
    try:
        _picks(monkeypatch, ("model-a", ""), ("model-z", ""))
        r = client.put("/api/chat/sessions/32/model", json={"model": "model-z"})
        assert r.json()["switch"] == "next_step"
    finally:
        chat_cancel.finish(32)


def test_a_pick_with_no_turn_running_records_nothing(client, monkeypatch):
    _picks(monkeypatch, ("model-a", ""), ("model-z", ""))
    r = client.put("/api/chat/sessions/33/model", json={"model": "model-z"})
    assert "switch" not in r.json()
    assert model_switch.latest(33) is None


def test_the_same_id_on_another_server_is_a_switch(client, monkeypatch):
    from aiforge_core.runtime import chat_cancel
    chat_cancel.start(34)
    try:
        _picks(monkeypatch, ("model-a", "http://box:1234/v1"),
               ("model-a", "http://box2:1234/v1"))
        r = client.put("/api/chat/sessions/34/model",
                       json={"model": "model-a", "base_url": "http://box2:1234/v1"})
        assert r.json()["switch"] == "next_step"
        assert model_switch.latest(34)["to_url"] == "http://box2:1234/v1"
    finally:
        chat_cancel.finish(34)


def test_failover_settings_round_trip(client, monkeypatch):
    from aiforge_core.config import model_registry
    rows = {"b": {"id": "b", "model": "model-b"}, "c": {"id": "c", "model": "model-c"}}
    monkeypatch.setattr(model_registry, "get_model", lambda mid: rows.get(mid))
    monkeypatch.setattr(model_registry, "list_models",
                        lambda: [{"id": k, "label": k, "model": v["model"]}
                                 for k, v in rows.items()])
    got = client.get("/api/chat/failover").json()
    assert got["models"] == [] and got["stall_s"] == 180 and got["fails"] == 2
    assert [o["id"] for o in got["options"]] == ["b", "c"]
    r = client.put("/api/chat/failover", json={"models": ["c", "b"], "stall_s": 120})
    assert r.status_code == 200
    assert r.json()["models"] == ["c", "b"] and r.json()["stall_s"] == 120
    assert client.put("/api/chat/failover", json={"models": ["nope"]}).status_code == 400
    assert client.put("/api/chat/failover",
                      json={"models": ["b"], "stall_s": 5}).status_code == 400


def test_the_env_threshold_applies_until_one_is_saved(client, monkeypatch):
    from aiforge_core.config import chat_failover
    monkeypatch.setenv("AIFORGE_LLM_FAILOVER_STALL_S", "150")
    assert chat_failover.stall_s() == 150
    monkeypatch.setenv("AIFORGE_LLM_FAILOVER", "off")
    assert chat_failover.view()["enabled"] is False
