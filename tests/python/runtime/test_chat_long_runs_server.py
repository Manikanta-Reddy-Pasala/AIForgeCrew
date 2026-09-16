"""What a multi-hour chat turn needs around the loop: its task kept in view,
a crash that does not lose it, an unanswered approval that does not end it,
and a resume that finds it again."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._context import _compaction
from aiforge_core.runtime.chat_agent._turn import _tasks

# ── what a condense keeps in view ────────────────────────────────────────

def test_the_pin_is_this_turns_task_with_steers_and_changed_files(tmp_path):
    st = SimpleNamespace(goal="build the report", steers=["use CSV, not JSON"],
                         cwd=str(tmp_path),
                         file_hashes={str(tmp_path / "src" / "a.py"): "x"})
    pin = _tasks.turn_pin(st)
    assert "build the report" in pin
    assert "- use CSV, not JSON" in pin
    assert "FILES CHANGED SO FAR: src/a.py" in pin


def test_no_goal_leaves_the_compactor_to_pick_one():
    assert _tasks.turn_pin(SimpleNamespace(goal="", steers=[], cwd="")) is None


def test_a_later_turns_condense_pins_that_turns_task():
    convo = [{"role": "system", "content": "sys"},
             {"role": "user", "content": "turn one: write the parser"},
             {"role": "assistant", "content": "done"},
             {"role": "user", "content": "turn two: add caching"}]
    text = _compaction._pin_goal("sys", convo, "ORIGINAL TASK:\nturn two: add caching")
    assert "turn two" in text and "turn one" not in text
    again = _compaction._pin_goal(text, convo, "ORIGINAL TASK:\nturn two, updated")
    assert again.count(_compaction._GOAL_PIN_OPEN) == 1
    assert "turn two, updated" in again


@pytest.mark.parametrize("note", [
    "OBSERVATION: {\"ok\": true}",
    "NOTE: 2 of the tool calls did not run",
    "[loop guard — not the user] You repeated the SAME output",
    "[task board — not the user] These items are still open",
    "[system reminder] You have gathered enough detail.",
    "[automated syntax check — not the user] The file has an error",
])
def test_loop_notes_are_not_remembered_as_user_asks(note):
    _, asks, _ = _compaction._middle_signals([{"role": "user", "content": note}])
    assert asks == []


def test_real_user_words_are_remembered():
    _, asks, _ = _compaction._middle_signals(
        [{"role": "user", "content": "[urgent] also fix the login page"}])
    assert asks == ["[urgent] also fix the login page"]


# ── approvals nobody answers ─────────────────────────────────────────────

def test_an_unanswered_approval_does_not_end_the_run():
    from aiforge_core.runtime.chat_agent._turn import _approval
    convo = []
    gen = _approval._handle_rejection(
        "run_command", {"cmd": "deploy"}, 5, convo,
        {"decision": "reject", "note": "approval timed out"})
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        sig = stop.value
    assert sig == "continue"
    assert events[0]["result"]["approval_timed_out"] is True
    assert "did not run" in convo[-1]["content"]
    assert not any(e.get("awaiting_input") for e in events)


def test_a_real_rejection_still_stops():
    from aiforge_core.runtime.chat_agent._turn import _approval
    gen = _approval._handle_rejection("run_command", {"cmd": "x"}, 5, [],
                                      {"decision": "reject", "note": ""})
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        assert stop.value == "return"
    assert any(e.get("awaiting_input") for e in events)


# ── crashes ──────────────────────────────────────────────────────────────

@pytest.fixture
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "cfg" / "chat.db"))
    from aiforge_core.runtime import chat_store
    chat_store.reset_backend_for_tests()
    yield chat_store
    chat_store.reset_backend_for_tests()


def test_a_crashed_turn_comes_back_as_a_stopped_turn(_store, monkeypatch):
    from aiforge_core.runtime import chat_resume, chat_turn_save
    monkeypatch.setenv("AIFORGE_CHAT_SAVE_EVERY_S", "10")
    sid = _store.create_session("t")["id"]
    _store.add_message(sid, "user", "migrate the database")
    saver = chat_turn_save.TurnSaver(sid, "simple")
    steps = [{"type": "tool", "name": "file_write", "args": {"path": "m.sql"},
              "result": {"ok": True}}]
    saver.save(steps, [{"slug": "a", "goal": "schema", "status": "done"}])
    # …the server dies here; at the next boot:
    assert chat_turn_save.recover_all() == 1
    rows = _store.get_messages(sid)
    assert rows[-1]["role"] == "assistant"
    assert "interrupted" in rows[-1]["content"]
    found = chat_resume.last_stopped_turn(rows)
    assert found and found[1] == "migrate the database"
    assert chat_turn_save.recover_all() == 0            # the file is gone


def test_a_finished_turn_leaves_nothing_behind(_store):
    from aiforge_core.runtime import chat_turn_save
    sid = _store.create_session("t")["id"]
    saver = chat_turn_save.TurnSaver(sid)
    saver.save([{"type": "thought", "text": "x"}], [])
    saver.discard()
    assert chat_turn_save.recover_all() == 0


def test_saving_waits_for_the_interval(_store, monkeypatch):
    from aiforge_core.runtime import chat_turn_save
    sid = _store.create_session("t")["id"]
    saver = chat_turn_save.TurnSaver(sid)
    saver.maybe_save([{"type": "thought", "text": "x"}], [])
    assert not chat_turn_save._path(sid).exists()
    saver._last -= 1000
    saver.maybe_save([{"type": "thought", "text": "x"}], [])
    assert json.loads(chat_turn_save._path(sid).read_text())["steps"]


# ── resuming ─────────────────────────────────────────────────────────────

def _stopped_rows(errors):
    steps = [{"type": "tool", "name": "run_command", "args": {"cmd": f"step {i}"},
              "result": {"ok": True}} for i in range(8)]
    steps += [{"type": "error", "text": e} for e in errors]
    steps.append({"type": "stopped", "reason": "cancelled"})
    return [{"role": "user", "content": "port the service"},
            {"role": "assistant", "content": "", "steps": steps}]


@pytest.mark.parametrize("prompt", ["continue", "Please continue.", "keep going",
                                    "resume from where you left off", "retry"])
def test_saying_continue_resumes_a_stopped_turn(prompt):
    from aiforge_core.runtime import chat_resume
    assert chat_resume.resume_preamble(_stopped_rows(["boom"]), prompt).startswith("[RESUME]")


def test_a_new_request_is_not_a_resume():
    from aiforge_core.runtime import chat_resume
    assert chat_resume.resume_preamble(_stopped_rows([]), "continue the report on X") == ""


def test_the_brief_keeps_the_latest_errors_and_commands():
    from aiforge_core.runtime import chat_resume
    brief = chat_resume.build_brief(
        _stopped_rows([f"error {i}" for i in range(6)])[1])
    assert "error 5" in brief and "error 0" not in brief
    assert "step 7" in brief and "step 0" not in brief


# ── producer slots ───────────────────────────────────────────────────────

def test_stop_while_waiting_for_a_slot(monkeypatch):
    from aiforge_core.api.routes._chat import _producer
    from aiforge_core.runtime import chat_cancel
    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    monkeypatch.setattr(_producer, "_PRODUCE_SEM", sem)
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: True)
    published = []
    run = SimpleNamespace(publish=published.append, finish=lambda: published.append("fin"))
    assert _producer._wait_for_slot(SimpleNamespace(run=run, session_id=3)) is False
    assert published[0]["type"] == "thought"
    assert {"type": "done"} in published and published[-1] == "fin"


def test_a_free_slot_is_taken_at_once(monkeypatch):
    from aiforge_core.api.routes._chat import _producer
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(_producer, "_PRODUCE_SEM", sem)
    run = SimpleNamespace(publish=lambda ev: pytest.fail("no wait message"))
    assert _producer._wait_for_slot(SimpleNamespace(run=run, session_id=3))


# ── background services ──────────────────────────────────────────────────

def test_an_expired_service_is_still_listed(monkeypatch):
    from aiforge_core.runtime.tools import serve
    proc = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(serve, "_SERVICES", {
        42: {"proc": proc, "cmd": "npm run dev", "url": "http://x", "ttl": 1,
             "started_at": 0.0, "pgid": None}})
    monkeypatch.setattr(serve, "_EXPIRED", {})
    monkeypatch.setattr(serve, "_kill_pgid", lambda pid, pgid: None)
    listed = serve.list_services()["services"]
    assert listed == [{"pid": 42, "url": "http://x", "cmd": "npm run dev",
                       "alive": False, "stopped": "ran past its ttl_s (1s)"}]
