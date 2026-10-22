"""Scope net: files the model changed under work it added itself are named
at FINAL; files under the request's items are not."""
from __future__ import annotations

import types

import pytest

from aiforge_core.runtime.chat_agent._guards import scope_net as SN
from aiforge_core.runtime.chat_agent._turn import _items, _tasks


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_SCOPE_NET", "1")


def test_model_added_items_are_tagged():
    b = _tasks.seed_board(["add login", "add logout", "write docs"])
    res, _ = _tasks.apply_progress(b, {"slug": "refactor-utils", "title": "refactor utils", "status": "running"})
    assert res["ok"] and b["refactor-utils"]["self_added"] is True
    assert b["part-1"].get("self_added") is None
    rows = {r["slug"]: r for r in _tasks.board_items(b)}
    assert rows["refactor-utils"]["self_added"] is True
    assert rows["part-1"]["self_added"] is False


def test_a_reworded_request_item_is_not_self_added():
    b = _tasks.seed_board(["add login endpoint", "add logout endpoint", "write docs"])
    res, _ = _tasks.apply_progress(b, {"slug": "login", "title": "add the login endpoint", "status": "running"})
    assert "login" not in b and b["part-1"].get("self_added") is None


def test_harness_adds_are_not_self_added():
    b = _tasks.seed_board(["a", "b", "c"])
    _tasks.apply_progress(b, {"slug": "steer-1", "title": "also bump the version", "by_harness": True})
    assert b["steer-1"].get("self_added") is None
    _tasks.apply_progress(b, {"slug": "repos", "title": "repositories for this chat"})
    assert b["repos"].get("self_added") is None


def test_unattached_files_come_from_self_added_windows():
    b = _tasks.seed_board(["a", "b", "c"])
    b["extra"] = {"title": "tidy utils", "status": "done", "self_added": True, "files": ["src/util.py", "README.md"]}
    b["part-1"]["status"] = "done"; b["part-1"]["files"] = ["src/login.py", "README.md"]
    st = types.SimpleNamespace(board=b, cwd="/r")
    assert SN.unrequested(st, {"src/login.py", "src/util.py", "README.md", "tests/t.py"}) == ["src/util.py"]


def test_nothing_flagged_without_self_added_items():
    b = _tasks.seed_board(["a", "b", "c"])
    st = types.SimpleNamespace(board=b, cwd="/r")
    assert SN.unrequested(st, {"x.py"}) == []
    assert SN.unrequested(types.SimpleNamespace(), {"x.py"}) == []
    assert SN.unrequested(types.SimpleNamespace(board=b), None) == []


def test_block_text_and_event():
    st = types.SimpleNamespace(board={}, cwd="/r", head0="abc123", session_id=9)
    assert SN.block([]) == ""
    assert SN.block(["src/util.py"]) == "\n\n⚠ Unrequested changes: `src/util.py`"
    assert SN.event(st, ["src/util.py"]) == {"type": "scope_net", "paths": ["src/util.py"], "head0": "abc123"}


def test_switch(monkeypatch):
    assert SN.enabled()
    monkeypatch.setenv("AIFORGE_CHAT_SCOPE_NET", "0")
    assert not SN.enabled()
    monkeypatch.setenv("AIFORGE_CHAT_SCOPE_NET", "off")
    assert not SN.enabled()


def test_review_progress_records_item_files(monkeypatch):
    # Item logging runs only with the evidence check (or the context reset)
    # on; the one edit in the window is the evidence that closes the item.
    monkeypatch.setenv("AIFORGE_CHAT_ITEM_VERIFY", "1")
    b = _tasks.seed_board(["a", "b", "c"])
    st = types.SimpleNamespace(board=b, cwd="/r", item_log=[("edit", ["/r/src/a.py"])], item_start=0,
                               item_rejects={}, item_prev_start=0, item_last_closed=None,
                               readonly_mode=False, builder="", item_seen={}, file_hashes={}, tree_hashes={})
    b["part-1"]["status"] = "done"
    _items.review_progress(st, {"ok": True, "slug": "part-1", "status": "done"}, [])
    assert b["part-1"]["files"] == ["src/a.py"]
