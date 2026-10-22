"""Scope net: files the model changed under work it added itself are named
at FINAL; files under the request's items are not."""
from __future__ import annotations

import subprocess
import types

import pytest

from aiforge_core.runtime.chat_agent._guards import scope_net as SN
from aiforge_core.runtime.chat_agent._turn import _items, _tasks


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_SCOPE_NET", "1")


def _ctx(board, goal="", steers=None):
    return _tasks.context_words(board, goal, steers)


def test_model_added_items_are_tagged():
    b = _tasks.seed_board(["add login", "add logout", "write docs"])
    res, _ = _tasks.apply_progress(b, {"slug": "refactor-utils", "title": "refactor utils", "status": "running"},
                                   context_words=_ctx(b, "add login, add logout, write docs"))
    assert res["ok"] and b["refactor-utils"]["self_added"] is True
    assert b["part-1"].get("self_added") is None
    rows = {r["slug"]: r for r in _tasks.board_items(b)}
    assert rows["refactor-utils"]["self_added"] is True
    assert rows["part-1"]["self_added"] is False


def test_without_context_words_nothing_is_tagged():
    b = _tasks.seed_board(["a", "b", "c"])
    _tasks.apply_progress(b, {"slug": "refactor-utils", "title": "refactor utils"})
    assert b["refactor-utils"].get("self_added") is None


def test_a_decomposed_big_request_is_not_self_added():
    """The prompt tells the model to write a big request as small tasks on
    its board; with the harness board empty, none of them is a rewording of
    a harness item, but each shares the request's words."""
    goal = "add login + logout + docs"
    b: dict = {}
    for slug, title in (("login", "add login endpoint"), ("logout", "add logout endpoint"),
                        ("docs", "write the docs")):
        _tasks.apply_progress(b, {"slug": slug, "title": title}, context_words=_ctx(b, goal))
    assert [s for s in b if b[s].get("self_added")] == []
    _tasks.apply_progress(b, {"slug": "tidy", "title": "refactor utils"}, context_words=_ctx(b, goal))
    assert b["tidy"]["self_added"] is True


def test_an_item_after_a_steer_shares_the_steer_words():
    b = _tasks.seed_board(["add login", "add logout", "write docs"])
    steers = ["also bump the version in setup.py"]
    _tasks.apply_progress(b, {"slug": "bump", "title": "bump version"},
                          context_words=_ctx(b, "add login, add logout, write docs", steers))
    assert b["bump"].get("self_added") is None
    _tasks.apply_progress(b, {"slug": "tidy", "title": "refactor utils"},
                          context_words=_ctx(b, "add login, add logout, write docs", steers))
    assert b["tidy"]["self_added"] is True


def test_harness_item_titles_count_as_context():
    b = _tasks.seed_board(["a", "b", "c"])
    b["steer-1"] = {"title": "rename the config loader", "status": "pending", "by_harness": True}
    _tasks.apply_progress(b, {"slug": "loader", "title": "config loader renamed"}, context_words=_ctx(b, ""))
    assert b["loader"].get("self_added") is None


def test_a_reworded_request_item_is_not_self_added():
    b = _tasks.seed_board(["add login endpoint", "add logout endpoint", "write docs"])
    res, _ = _tasks.apply_progress(b, {"slug": "login", "title": "add the login endpoint", "status": "running"},
                                   context_words=_ctx(b))
    assert "login" not in b and b["part-1"].get("self_added") is None


def test_harness_adds_are_not_self_added():
    b = _tasks.seed_board(["a", "b", "c"])
    _tasks.apply_progress(b, {"slug": "steer-1", "title": "also bump the version", "by_harness": True},
                          context_words=_ctx(b))
    assert b["steer-1"].get("self_added") is None
    _tasks.apply_progress(b, {"slug": "repos", "title": "repositories for this chat"}, context_words=_ctx(b))
    assert b["repos"].get("self_added") is None


def test_unattached_files_come_from_self_added_windows():
    b = _tasks.seed_board(["a", "b", "c"])
    b["extra"] = {"title": "tidy utils", "status": "done", "self_added": True, "files": ["src/util.py", "README.md"]}
    b["part-1"]["status"] = "done"; b["part-1"]["files"] = ["src/login.py", "README.md"]
    st = types.SimpleNamespace(board=b, cwd="/r")
    assert SN.unrequested(st, {"src/login.py", "src/util.py", "README.md", "tests/t.py"}) == ["src/util.py"]


def test_nothing_flagged_when_no_request_item_recorded_files():
    """Request items the harness closed at FINAL carry no files: with nothing
    to compare against, the net stays quiet."""
    b = _tasks.seed_board(["a", "b", "c"])
    b["extra"] = {"title": "tidy utils", "status": "done", "self_added": True, "files": ["src/util.py"]}
    st = types.SimpleNamespace(board=b, cwd="/r")
    assert SN.unrequested(st, {"src/util.py"}) == []
    b["part-1"]["files"] = []
    assert SN.unrequested(st, {"src/util.py"}) == []
    b["part-1"]["files"] = ["src/a.py"]
    assert SN.unrequested(st, {"src/util.py"}) == ["src/util.py"]


def test_nothing_flagged_without_self_added_items():
    b = _tasks.seed_board(["a", "b", "c"])
    b["part-1"]["files"] = ["x.py"]
    st = types.SimpleNamespace(board=b, cwd="/r")
    assert SN.unrequested(st, {"x.py"}) == []
    assert SN.unrequested(types.SimpleNamespace(), {"x.py"}) == []
    assert SN.unrequested(types.SimpleNamespace(board=b), None) == []


def test_sub_folder_cwd_matches_toplevel_relative_changes():
    """Item files are relative to the work folder, git's changed set to the
    repository; a chat run from a sub-folder still matches, and the names
    given back are the item's own."""
    b = _tasks.seed_board(["a", "b", "c"])
    b["extra"] = {"title": "tidy", "status": "done", "self_added": True, "files": ["util.py", "../top.py"]}
    b["part-1"]["files"] = ["login.py"]
    st = types.SimpleNamespace(board=b, cwd="/r/sub")
    changed = {"sub/util.py", "sub/login.py", "top.py"}
    assert SN.unrequested(st, changed, "sub/") == ["../top.py", "util.py"]
    assert SN.unrequested(st, changed, "") == []


def test_prefix_of_reads_git(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "sub").mkdir()
    assert SN.prefix_of(str(tmp_path)) == ""
    assert SN.prefix_of(str(tmp_path / "sub")) == "sub/"
    assert SN.prefix_of(None) == ""


def test_block_text_and_event():
    st = types.SimpleNamespace(board={}, cwd="/r", head0="abc123", session_id=9)
    assert SN.block([]) == ""
    assert SN.block(["src/util.py"]) == "\n\n⚠ Unrequested changes: `src/util.py`"
    assert SN.event(st, ["src/util.py"]) == {"type": "scope_net", "paths": ["src/util.py"], "head0": "abc123"}


def test_block_caps_the_list():
    many = [f"f{i}.py" for i in range(15)]
    text = SN.block(many)
    assert text.endswith("`f11.py` (+3 more)") and "f12.py" not in text


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
