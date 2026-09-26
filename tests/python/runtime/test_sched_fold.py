"""A message typed while a scheduled agent or a watch owns the chat.

It stops the run only on an explicit stop, steers it on an extra detail,
and otherwise runs as a normal turn. It is never swallowed."""
from __future__ import annotations

import threading
import types

import pytest

from aiforge_core.jobs import scheduler, store
from aiforge_core.runtime import bg_work, chat_interject, chat_store
from aiforge_core.api.routes._chat import _sched_fold as F


@pytest.fixture
def sid(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_BG_DB_PATH", str(tmp_path / "background.db"))
    store._BACKEND = None
    chat_store.reset_backend_for_tests()
    bg_work._SCHEMA_DONE.clear()
    scheduler._STOP.clear()
    scheduler._SESSION_AGENT.clear()
    s = chat_store.create_session("fold")["id"]
    ev = threading.Event()
    scheduler._STOP[91] = ev
    scheduler._SESSION_AGENT[s] = 91
    chat_interject.clear(s)
    chat_interject.set_steerable(s, True)
    s_ns = types.SimpleNamespace(id=s, stop=ev)
    yield s_ns
    chat_interject.clear(s)
    chat_interject.set_steerable(s, False)
    scheduler._STOP.clear()
    scheduler._SESSION_AGENT.clear()
    store._BACKEND = None
    chat_store.reset_backend_for_tests()


def _texts(s):
    return [m.get("content") or "" for m in chat_store.get_messages(s)]


def _body(**kw):
    base = dict(edit_from_message_id=None, quick=False, resume=None,
                builder=None, mode="simple")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_an_extra_detail_steers_the_run(sid):
    assert F._fold_into_scheduled_agent(sid.id, "also log the date") is not None
    assert chat_interject.pending(sid.id)
    assert sid.stop.is_set() is False
    assert "also log the date" in _texts(sid.id)


def test_a_polite_instruction_still_steers(sid):
    got = F._fold_into_scheduled_agent(sid.id, "can you also log the date?")
    assert got is not None
    assert chat_interject.pending(sid.id)


@pytest.mark.parametrize("text", [
    "add a Cancel button", "how do I kill the process on :3000",
])
def test_a_stop_word_that_is_not_a_stop_does_not_end_the_run(sid, text):
    F._fold_into_scheduled_agent(sid.id, text)
    assert sid.stop.is_set() is False


def test_an_explicit_stop_ends_the_run(sid):
    assert F._fold_into_scheduled_agent(sid.id, "cancel the scheduled run")
    assert sid.stop.is_set() is True


def test_a_run_that_no_longer_reads_messages_hands_over_to_a_normal_turn(sid):
    """push() refused: the run is ending. The message must not be claimed."""
    chat_interject.set_steerable(sid.id, False)
    assert F._fold_into_scheduled_agent(sid.id, "also log the date") is None
    assert not chat_interject.pending(sid.id)
    assert _texts(sid.id) == []   # the normal turn persists it, once


@pytest.mark.parametrize("text", [
    "what did the last run print?",
    "how does the scheduler pick a time",
    "new task: write a README",
    "why is it slow?",
])
def test_a_question_or_new_request_is_a_normal_turn(sid, text):
    assert F._fold_into_scheduled_agent(sid.id, text) is None
    assert not chat_interject.pending(sid.id)
    assert sid.stop.is_set() is False


@pytest.mark.parametrize("opts", [
    {"quick": True}, {"edit_from_message_id": 4}, {"resume": True},
    {"builder": "job"}, {"mode": "plan"}, {"mode": "team"},
])
def test_per_turn_options_are_never_folded_away(sid, opts):
    assert F._fold_into_scheduled_agent(
        sid.id, "also log the date", _body(**opts)) is None
    assert not chat_interject.pending(sid.id)


def test_a_fresh_attachment_is_never_folded_away(sid, monkeypatch):
    chat_store.add_message(sid.id, "assistant", "earlier")
    monkeypatch.setattr(chat_store, "list_media", lambda s: [
        {"id": 1, "created_at": "9999-01-01T00:00:00.000Z"}])
    assert F._fold_into_scheduled_agent(
        sid.id, "use this screenshot", _body()) is None


def test_an_older_attachment_does_not_block_a_steer(sid, monkeypatch):
    chat_store.add_message(sid.id, "assistant", "earlier")
    monkeypatch.setattr(chat_store, "list_media", lambda s: [
        {"id": 1, "created_at": "2000-01-01T00:00:00.000Z"}])
    assert F._fold_into_scheduled_agent(
        sid.id, "also log the date", _body()) is not None


def test_nothing_running_is_a_normal_turn(sid):
    scheduler._SESSION_AGENT.clear()
    assert F._fold_into_scheduled_agent(sid.id, "stop") is None


def test_a_cancel_button_request_leaves_background_watches_running(sid):
    wid = bg_work._insert(sid.id, "watch", "/tmp", {"args": {"cmd": "true"}})
    F._cut_background_watches(sid.id, "add a Cancel button to the form")
    F._cut_background_watches(sid.id, "how do I kill the process on :3000")
    assert bg_work._get(wid)["status"] == "running"
    F._cut_background_watches(sid.id, "stop watching")
    assert bg_work._get(wid)["status"] == "stopped"
