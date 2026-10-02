"""Asking "what is the status?" while a run goes on is answered from the run.

Before: it was queued as a steer (read at the agent's next step, often minutes
away, and the reply could be discarded) or started as a side agent that knew
nothing about the run. The run already records what it is doing; the answer is
built from that, at once, without a model, and nothing is interrupted.
"""
from __future__ import annotations

import threading
import time

import pytest

from aiforge_core.runtime import chat_status


@pytest.mark.parametrize("text", [
    "status", "status?", "Status please", "progress", "update", "eta",
    "what's the status?", "what is the status", "whats the progress",
    "what's happening?", "what is going on", "what are you doing?",
    "how far along are you?", "how long will it take?", "how much is left",
    "how is it going", "how's it going?", "are you still working?",
    "is it done?", "are you stuck", "is this still running", "are you alive",
    "any update?", "give me an update", "show me the status",
    "where are you at", "what step are you on?", "is anything happening?",
    "still there?", "STATUS!!",
])
def test_these_ask_how_the_run_is_going(text):
    assert chat_status.is_status_request(text) is True


@pytest.mark.parametrize("text", [
    "", "   ", "update the README file", "update the login handler to use jwt",
    "what is the capital of France?", "what does the retry helper do?",
    "add a status field to the user table", "fix the status code of the endpoint",
    "also handle the empty list case", "stop", "no, use the v2 endpoint instead",
    "run another agent to check the logs for errors",
    "what is the status of the invoice with number 4412 in the database "
    "and also send it to the customer by email today please",
])
def test_these_do_not(text):
    assert chat_status.is_status_request(text) is False


@pytest.fixture
def run(monkeypatch):
    from aiforge_core.runtime import chat_runs
    monkeypatch.setattr(chat_runs, "_ensure_watchdog", lambda: None)
    r = chat_runs.start(301)
    yield r
    chat_runs.finish_all()


def test_the_run_is_followed_as_events_go_by(run):
    run.publish({"type": "tool_start", "name": "read_file", "args": {"path": "src/a.py"}, "call_id": 1})
    run.publish({"type": "tool", "name": "read_file", "args": {"path": "src/a.py"},
                 "result": {"ok": True}, "call_id": 1})
    run.publish({"type": "tool_start", "name": "run_command",
                 "args": {"cmd": "pytest -q", "timeout": 90}, "call_id": 2})
    run.publish({"type": "tool", "name": "run_command", "args": {"cmd": "pytest -q"},
                 "result": {"ok": False, "error": "1 failed"}, "call_id": 2})
    run.publish({"type": "tool_start", "name": "command_wait", "args": {"id": "bg-6"}, "call_id": 3})
    run.publish({"type": "thought", "role": "doer", "text": "waiting for the build\nto finish"})
    run.publish({"type": "changes", "files": ["a", "b"],
                 "summary": {"files": 2, "additions": 14, "deletions": 3}})
    assert run.tool_count == 2
    assert [t["name"] for t in run.recent_tools] == ["read_file", "run_command"]
    assert run.recent_tools[0]["ok"] is True and run.recent_tools[1]["ok"] is False
    assert list(run.open_tools.values())[0]["name"] == "command_wait"
    assert run.last_thought == "waiting for the build to finish"
    assert run.changes == {"files": 2, "additions": 14, "deletions": 3}


def test_the_answer_says_what_it_is_on_what_it_did_and_how_long(run):
    run.publish({"type": "tool_start", "name": "write_file", "args": {"path": "src/cart.py"}, "call_id": 1})
    run.publish({"type": "tool", "name": "write_file", "args": {"path": "src/cart.py"},
                 "result": {"ok": True}, "call_id": 1})
    run.publish({"type": "tool_start", "name": "run_command", "args": {"cmd": "sleep 70"}, "call_id": 2})
    run.started_at -= 125
    run.last_event_at -= 41
    text = chat_status.render(chat_status.snapshot(run, pending_steers=1))
    assert text.startswith("**Status** — working for 2m 05s · 1 tool call done")
    assert "**Now:** `run_command` `sleep 70`" in text
    assert "**Just done:** `write_file` `cart.py` ✓" in text
    assert "No new output for 41s — that is normal while a command runs." in text
    assert "1 message from you waiting to be read." in text
    assert "Nothing was interrupted" in text


def test_a_run_waiting_on_the_model_says_so(run):
    run.publish({"type": "thought", "role": "system",
                 "text": "⏸ waiting for model at http://x:1234 (down 40s, next probe 10s)"})
    run.last_event_at -= 50
    text = chat_status.render(chat_status.snapshot(run))
    assert "**Now:** ⏸ waiting for model at http://x:1234" in text
    assert "waiting on the model" in text


def test_a_finished_or_answered_run_is_reported_as_such(run):
    run.publish({"type": "done"})
    assert "the answer is out" in chat_status.render(chat_status.snapshot(run))
    run.finish()
    assert "has finished" in chat_status.render(chat_status.snapshot(run))


def test_a_steer_is_told_when_it_will_be_read_in_plain_words(run):
    assert chat_status.waiting_on(run) == "The agent will read your message at its next step."
    run.publish({"type": "tool_start", "name": "command_wait", "args": {"id": "bg-6"}, "call_id": 1})
    text = chat_status.waiting_on(run)
    assert "waiting for a command to finish" in text and "within seconds" in text
    assert "Nothing is stopped" in text
    assert "command_wait" not in text and "bg-6" not in text       # no internals
    run.publish({"type": "tool", "name": "command_wait", "args": {}, "result": {}, "call_id": 1})
    run.publish({"type": "delta", "phase": "answer", "text": "x"})
    assert "middle of an answer" in chat_status.waiting_on(run)


def test_a_steer_during_a_long_command_does_not_show_the_raw_command(run):
    cmd = ("cd /mnt/c/Users/Manikanta.Pasala/Documents/coderepo/ai_elint && "
           ".aiforge-venv/bin/python -m pytest tests/unit -x -q --maxfail=3 --disable-warnings")
    run.publish({"type": "tool_start", "name": "run_command", "args": {"cmd": cmd}, "call_id": 1})
    text = chat_status.waiting_on(run)
    assert text.startswith("The agent is running `python -m pytest tests/unit")
    assert "/mnt/c" not in text and "run_command" not in text and "aiforge-venv" not in text
    assert text.endswith("Nothing is stopped.")
    assert "…" in text or len(chat_status.friendly_cmd(cmd)) <= 60
    # nothing is cut in the middle of a word ("…/bi")
    assert not chat_status.friendly_cmd(cmd).endswith("/bi")


@pytest.mark.parametrize("cmd,want", [
    ("cd /a/b && npm test", "npm test"),
    ("cd /a/b; cd c && make build", "make build"),
    ("/usr/local/bin/python3 script.py --flag", "python3 script.py --flag"),
    ("echo hi", "echo hi"),
    ("", ""),
])
def test_friendly_cmd(cmd, want):
    assert chat_status.friendly_cmd(cmd) == want


def test_friendly_cmd_cuts_at_a_word():
    out = chat_status.friendly_cmd("pytest " + " ".join(f"tests/test_{i}.py" for i in range(30)), 40)
    assert out.endswith("…") and len(out) <= 40 and not out[:-1].endswith("test_")


def test_activity_in_words():
    d = chat_status.describe_activity
    assert d("write_file", "x.py") == "writing files"
    assert d("read_file", "x.py") == "reading the project"
    assert d("run_command", "cd /x && make") == "running `make`"
    assert d("run_command", "") == "running a command"
    assert d("some_new_tool") == "working with some new tool"


def test_a_leaked_tool_start_never_grows_without_bound(run):
    for i in range(40):
        run.publish({"type": "tool_start", "name": "t", "args": {}, "call_id": i})
    assert len(run.open_tools) <= 12
    run.publish({"type": "done"})
    assert run.open_tools == {}


# ── a message from the user wakes command_wait; the command keeps running ─────

@pytest.fixture
def job_env(monkeypatch, tmp_path):
    from aiforge_core.runtime import cmd_jobs
    from aiforge_core.runtime.chat_agent import _shell as S
    monkeypatch.setenv("AIFORGE_BG_DB_PATH", str(tmp_path / "bg.db"))
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "1")
    monkeypatch.setattr(S, "_workspace_root", lambda: None)
    turn = cmd_jobs.begin_turn()
    yield tmp_path, S, cmd_jobs
    cmd_jobs.end_turn(turn)


def test_command_wait_returns_at_once_for_any_message_and_the_job_lives(job_env):
    from aiforge_core.runtime import chat_interject
    tmp, S, cmd_jobs = job_env
    from aiforge_core.runtime import chat_cancel
    sid = 777
    chat_cancel.start(sid)
    chat_cancel.set_active(sid)
    res = S._t_run_command({"cmd": "sleep 30"}, str(tmp))
    assert res["running"] is True
    job = cmd_jobs.find(res["id"])
    chat_interject.set_steerable(sid, True)
    threading.Timer(0.5, lambda: chat_interject.push(
        sid, "also name the function apply_discount", require_steerable=True)).start()
    t0 = time.monotonic()
    got = cmd_jobs.wait(job, 20, session_id=sid)
    assert time.monotonic() - t0 < 5                  # not the 20s it asked for
    assert not got.get("steered") and not got.get("killed")
    assert got["ok"] is True and got["running"] is True
    assert got["user_message_pending"] is True
    # the model is told to carry on waiting, not to start the command again
    assert "STILL RUNNING" in got["hint"] and f"command_wait id={job.key}" in got["hint"]
    assert "do NOT start it again" in got["hint"]
    assert job.alive()                                # the command was not cut
    chat_interject.clear(sid)
    job.kill()


def test_a_stop_message_still_ends_the_command(job_env):
    from aiforge_core.runtime import chat_interject
    from aiforge_core.runtime import chat_cancel
    tmp, S, cmd_jobs = job_env
    sid = 778
    chat_cancel.start(sid)
    chat_cancel.set_active(sid)          # the chat run that owns the command
    res = S._t_run_command({"cmd": "sleep 30"}, str(tmp))
    job = cmd_jobs.find(res["id"])
    chat_interject.set_steerable(sid, True)
    chat_interject.push(sid, "stop", require_steerable=True)
    got = cmd_jobs.wait(job, 20, session_id=sid)
    assert got.get("steered") is True
    assert job.killed                                  # the stop wording killed it
    deadline = time.monotonic() + 5
    while job.alive() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not job.alive()
    chat_interject.clear(sid)


def test_the_command_behind_a_wait_is_named(run):
    run.publish({"type": "tool_start", "name": "command_wait",
                 "args": {"id": "bg-6"}, "call_id": 1})
    snap = chat_status.snapshot(run, jobs=[{"id": "bg-6", "cmd": "sleep 75",
                                            "for_s": 33.0, "idle_s": 31.0,
                                            "background": False}])
    text = chat_status.render(snap)
    assert "**Running command:** `sleep 75` — 33s so far, no output for 31s" in text
    assert "**The agent is:** in `command_wait` `bg-6`, waiting on it" in text


def test_tool_names_survive_markdown():
    # underscores in a bare name are read as emphasis; code spans are not
    assert chat_status._call("write_file", "/home/me/proj/src/cart.py") == "`write_file` `cart.py`"
    assert chat_status._call("grep", "") == "`grep`"


def test_a_message_that_replaces_the_work_still_pauses_it(job_env):
    from aiforge_core.runtime import chat_cancel, chat_interject
    tmp, S, cmd_jobs = job_env
    sid = 779
    chat_cancel.start(sid)
    chat_cancel.set_active(sid)
    res = S._t_run_command({"cmd": "sleep 30"}, str(tmp))
    job = cmd_jobs.find(res["id"])
    chat_interject.set_steerable(sid, True)
    chat_interject.push(sid, "scratch that, do something else instead", require_steerable=True)
    got = cmd_jobs.wait(job, 20, session_id=sid)
    assert got.get("steered") is True and "user_message_pending" not in got
    chat_interject.clear(sid)
    job.kill()


def test_a_commands_quiet_time_is_real_not_the_stuck_threshold(job_env):
    from aiforge_core.runtime import chat_cancel
    tmp, S, cmd_jobs = job_env
    sid = 780
    chat_cancel.start(sid)
    chat_cancel.set_active(sid)
    res = S._t_run_command({"cmd": "echo start; sleep 30"}, str(tmp))
    job = cmd_jobs.find(res["id"])
    time.sleep(1.2)
    (info,) = cmd_jobs.for_session(sid)
    assert info["cmd"] == "echo start; sleep 30" and info["id"] == job.key
    assert 0.5 < info["idle_s"] < 5          # not the 45s stuck threshold
    assert 1 < info["for_s"] < 8
    assert cmd_jobs.for_session(99999) == []
    job.kill()
