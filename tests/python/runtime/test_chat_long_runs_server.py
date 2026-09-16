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
    "[batch note — not the user] Some tool calls did not run",
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


def test_a_second_unanswered_approval_pauses_the_run():
    from aiforge_core.runtime.chat_agent._turn import _approval
    _approval._TIMEOUTS.clear()
    timeout = {"decision": "reject", "note": "approval timed out"}
    first = list(_approval._handle_rejection("a", {}, 9, [], timeout))
    second = list(_approval._handle_rejection("b", {}, 9, [], timeout))
    assert not any(e.get("awaiting_input") for e in first)
    assert any(e.get("awaiting_input") for e in second)
    assert {"type": "stopped", "reason": "approval_timeout"} in second


def test_a_late_click_on_an_expired_card_approves_nothing(monkeypatch):
    from aiforge_core.runtime import chat_approve
    monkeypatch.setattr(chat_approve, "_timeout_s", lambda: 0.01)
    first = chat_approve.request(77)
    assert chat_approve.wait(77)["note"] == "approval timed out"
    second = chat_approve.request(77)
    assert second != first
    assert chat_approve.resolve(77, "approve", seq=first) is False
    assert chat_approve.resolve(77, "approve", seq=second) is True
    assert chat_approve.wait(77)["decision"] == "approve"
    chat_approve.finish(77)


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
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "cfg" / "chat.db"))
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


def test_a_failed_recovery_keeps_the_file(_store, monkeypatch):
    from aiforge_core.runtime import chat_turn_save
    sid = _store.create_session("t")["id"]
    chat_turn_save.TurnSaver(sid).save([{"type": "thought", "text": "x"}], [])
    monkeypatch.setattr(_store, "add_message",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert chat_turn_save.recover_all() == 0
    assert chat_turn_save._path(sid).exists()


def test_a_turn_another_live_server_is_saving_is_left_alone(_store, monkeypatch):
    import os

    from aiforge_core.runtime import chat_turn_save
    sid = _store.create_session("t")["id"]
    chat_turn_save.TurnSaver(sid).save([{"type": "thought", "text": "x"}], [])
    path = chat_turn_save._path(sid)
    data = json.loads(path.read_text())
    data["pid"] = os.getppid()                  # alive, and not this process
    path.write_text(json.dumps(data))
    assert chat_turn_save.recover_all() == 0
    assert path.exists()
    old = path.stat().st_mtime - 3600           # …unless it stopped saving
    os.utime(path, (old, old))
    assert chat_turn_save.recover_all() == 1


def test_a_finished_turn_leaves_nothing_behind(_store):
    from aiforge_core.runtime import chat_turn_save
    sid = _store.create_session("t")["id"]
    saver = chat_turn_save.TurnSaver(sid)
    saver.save([{"type": "thought", "text": "x"}], [])
    saver.discard()
    assert chat_turn_save.recover_all() == 0


def test_a_huge_turn_keeps_its_start_and_end(_store):
    from aiforge_core.runtime import chat_turn_save
    sid = _store.create_session("t")["id"]
    steps = [{"type": "thought", "text": str(i)} for i in range(5000)]
    chat_turn_save.TurnSaver(sid).save(steps, [])
    kept = json.loads(chat_turn_save._path(sid).read_text())["steps"]
    assert len(kept) == 1551
    assert kept[0]["text"] == "0" and kept[-1]["text"] == "4999"


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


def test_continue_keeps_the_original_request_in_view():
    from aiforge_core.runtime import chat_resume
    from aiforge_core.runtime.chat_agent._turn._state import _turn_goal
    rows = _stopped_rows(["boom"])
    brief = chat_resume.resume_preamble(rows, "continue")
    assert "port the service" in brief
    message = {"role": "user", "content": f"continue\n\n---\n{brief}"}
    assert _turn_goal([message]) == "port the service"
    # a second "continue" still finds the real request
    rows2 = rows + [{"role": "user", "content": "continue"},
                    {"role": "assistant", "content": "", "steps": rows[1]["steps"]}]
    assert "port the service" in chat_resume.resume_preamble(rows2, "keep going")


def test_the_same_words_again_need_no_quote():
    from aiforge_core.runtime import chat_resume
    brief = chat_resume.resume_preamble(_stopped_rows(["boom"]), "port the service")
    assert chat_resume.REQUEST_OPEN not in brief


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
    listed = serve.list_services()
    assert listed["services"] == []
    assert listed["expired"] == [{"pid": 42, "url": "http://x", "cmd": "npm run dev",
                                  "alive": False, "stopped": "ran past its ttl_s (1s)"}]
    assert "expired" not in serve.list_services()          # reported once


def test_timeouts_in_an_earlier_turn_do_not_count():
    from aiforge_core.runtime.chat_agent._turn import _approval
    _approval._TIMEOUTS.clear()
    timeout = {"decision": "reject", "note": "approval timed out"}
    list(_approval._handle_rejection("a", {}, 11, [], timeout))
    assert _approval._TIMEOUTS[11] == 1
    _approval.new_turn(11)
    events = list(_approval._handle_rejection("b", {}, 11, [], timeout))
    assert not any(e.get("awaiting_input") for e in events)
