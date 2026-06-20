import importlib

import pytest


@pytest.fixture
def cs(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    import aiforge_core.runtime.chat_store as cs
    importlib.reload(cs)
    return cs


def test_create_and_list(cs):
    s = cs.create_session("first", cwd="/tmp")
    assert s["id"] >= 1
    assert s["title"] == "first"
    rows = cs.list_sessions()
    assert len(rows) == 1
    assert rows[0]["message_count"] == 0


def test_messages_roundtrip(cs):
    s = cs.create_session()
    cs.add_message(s["id"], "user", "hello")
    cs.add_message(s["id"], "assistant", "hi back",
                   steps=[{"type": "tool", "name": "file_read"}])
    msgs = cs.get_messages(s["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["steps"][0]["name"] == "file_read"


def test_rename_and_delete(cs):
    s = cs.create_session()
    cs.rename_session(s["id"], "renamed topic")
    assert cs.get_session(s["id"])["title"] == "renamed topic"
    assert cs.delete_session(s["id"]) is True
    assert cs.get_session(s["id"]) is None
    assert cs.list_sessions() == []


def test_delete_cascades_messages(cs):
    s = cs.create_session()
    cs.add_message(s["id"], "user", "x")
    cs.delete_session(s["id"])
    assert cs.get_messages(s["id"]) == []


def test_list_orders_by_recent(cs):
    import time
    a = cs.create_session("a")
    time.sleep(0.005)
    b = cs.create_session("b")
    time.sleep(0.005)
    cs.add_message(a["id"], "user", "bump a")  # touches updated_at → newest
    ids = [r["id"] for r in cs.list_sessions()]
    assert ids[0] == a["id"]
