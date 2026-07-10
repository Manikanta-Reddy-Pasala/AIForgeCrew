"""The run mode (simple|plan|team) is persisted per user turn and surfaced as
the session's last_mode — so the UI can badge which mode a session ran in
(was composer-only client state, all sessions looked identical = 'simple chat').
"""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture()
def store(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", d)
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", d + "/chat.db")
    from aiforge_core.runtime import chat_store
    return chat_store


def test_user_turn_mode_persisted(store):
    s = store.create_session("e2e-plan")
    store.add_message(s["id"], "user", "make a plan", mode="plan")
    store.add_message(s["id"], "assistant", "here it is")
    modes = [(m["role"], m["mode"]) for m in store.get_messages(s["id"])]
    assert modes == [("user", "plan"), ("assistant", "simple")]


def test_default_mode_is_simple(store):
    s = store.create_session("plain")
    store.add_message(s["id"], "user", "hi")            # no mode arg
    assert store.get_messages(s["id"])[0]["mode"] == "simple"


def test_session_last_mode_from_latest_user_turn(store):
    a = store.create_session("e2e-team")
    store.add_message(a["id"], "user", "build it", mode="team")
    b = store.create_session("e2e-plan")
    store.add_message(b["id"], "user", "plan it", mode="plan")
    c = store.create_session("e2e-simple")
    store.add_message(c["id"], "user", "chat")
    by_title = {x["title"]: x.get("last_mode") for x in store.list_sessions()}
    assert by_title["e2e-team"] == "team"
    assert by_title["e2e-plan"] == "plan"
    assert by_title["e2e-simple"] == "simple"


def test_last_mode_follows_most_recent_turn(store):
    s = store.create_session("switcher")
    store.add_message(s["id"], "user", "first", mode="team")
    store.add_message(s["id"], "user", "second", mode="plan")
    last = {x["title"]: x.get("last_mode") for x in store.list_sessions()}
    assert last["switcher"] == "plan"      # latest user turn wins
