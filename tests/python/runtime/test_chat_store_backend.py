"""chat_store backend selection + SQLite round-trip.

SQLite-only build: the backend is always the embedded SQLite impl. Here we
assert the factory picks it and that the SQLite path round-trips end to end.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def cs(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    import aiforge_core.runtime.chat_store as m
    importlib.reload(m)
    from aiforge_core.config import env
    monkeypatch.setattr(env, "AIFORGE_USE_SQLITE", True, raising=False)
    monkeypatch.setattr(env, "AIFORGE_PG_URL", None, raising=False)
    m.reset_backend_for_tests()
    return m


def test_sqlite_selected_when_no_pg(cs):
    assert isinstance(cs._backend(), cs._SqliteChatStore)
    assert cs._backend().name == "sqlite"




def test_sqlite_session_message_roundtrip(cs):
    s = cs.create_session(title="Hello", role="chat")
    assert s["id"] > 0
    assert s["title"] == "Hello"
    assert s["role"] == "chat"
    mid = cs.add_message(s["id"], "user", "hi there", steps=[{"k": "v"}])
    assert mid > 0
    msgs = cs.get_messages(s["id"])
    assert len(msgs) == 1
    assert msgs[0]["content"] == "hi there"
    assert msgs[0]["steps"] == [{"k": "v"}]
    got = cs.get_session(s["id"])
    assert got["id"] == s["id"]
    sessions = cs.list_sessions()
    assert sessions[0]["message_count"] == 1


def test_sqlite_search_roundtrip(cs):
    s = cs.create_session(title="Kafka")
    cs.add_message(s["id"], "assistant", "use a dead-letter topic for kafka retries")
    hits = cs.search_messages("kafka retries")
    assert hits
    assert hits[0]["content"].startswith("use a dead-letter")


def test_sqlite_media_roundtrip(cs):
    s = cs.create_session()
    m = cs.add_media(s["id"], "a.png", "/tmp/a.png", mime="image/png", description="d")
    assert m["filename"] == "a.png"
    lst = cs.list_media(s["id"])
    assert len(lst) == 1
    assert cs.get_media(m["id"])["path"] == "/tmp/a.png"


def test_sqlite_checkpoint_and_truncate(cs):
    s = cs.create_session()
    m1 = cs.add_message(s["id"], "user", "one")
    cs.set_message_checkpoint(m1, "sha123")
    assert cs.message_checkpoint(s["id"], m1) == "sha123"
    cs.add_message(s["id"], "assistant", "two")
    removed = cs.delete_messages_from(s["id"], m1)
    assert removed == 2
    assert cs.get_messages(s["id"]) == []
