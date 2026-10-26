"""The Processes dock shows what runs, live, and files the rest under tasks.

* a foreground run_command is listed (and tailed) from its first second,
  not only once handed off at the check-in;
* a tile opened on it keeps reading the job it is handed off as;
* Python output is not held in an 8 KB buffer;
* every command carries the task-board item it ran under."""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from aiforge_core.runtime import chat_cancel, cmd_jobs, proc_logs
from aiforge_core.runtime.chat_agent import _shell as S
from aiforge_core.runtime.chat_agent._turn import _tool_dispatch as D

SID = 4343
PY = sys.executable


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_BG_DB_PATH", str(tmp_path / "bg.db"))
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "60")
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "600")
    monkeypatch.delenv("AIFORGE_CHAT_CMD_TIMEOUT_S", raising=False)
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    monkeypatch.setattr(S, "_workspace_root", lambda: None)
    monkeypatch.setattr(chat_cancel, "active", lambda: SID)
    proc_logs._reset_for_tests()
    turn = cmd_jobs.begin_turn()
    yield tmp_path
    for job in list(cmd_jobs._JOBS.values()):
        if str(job.session_id) == str(SID):
            job.kill("test over")
            cmd_jobs._forget(job)
    cmd_jobs.end_turn(turn)
    proc_logs._reset_for_tests()


def _until(pred, s=10.0):
    end = time.monotonic() + s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _run_in_thread(args, cwd):
    box = {}

    def go():
        box["res"] = S._t_run_command(args, str(cwd))
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, box


def _fg_rows():
    return [r for r in proc_logs.list_for_session(SID) if r["id"].startswith("fg-")]


def test_a_foreground_command_streams_before_it_ends(env):
    # Python writing to a file would buffer "first" until exit.
    cmd = (f"{PY} -c \"import time; print('first'); time.sleep(3); "
           f"print('second')\"")
    t, box = _run_in_thread({"cmd": cmd, "description": "Two lines"}, env)
    assert _until(lambda: bool(_fg_rows()))
    row = _fg_rows()[0]
    assert row["running"] is True and row["label"] == "Two lines"
    key = row["id"]
    assert _until(lambda: "first" in proc_logs.read(SID, key)["streams"][0]["text"],
                  2.5)
    assert t.is_alive()                       # seen while it still runs
    t.join(15)
    assert box["res"]["ok"] is True
    ended = proc_logs.list_for_session(SID)
    assert [r["id"] for r in ended] == [key]
    assert ended[0]["running"] is False and ended[0]["code"] == 0
    assert "second" in proc_logs.read(SID, key)["streams"][0]["text"]
    assert proc_logs._FOREGROUND == {}


def test_a_handed_off_command_keeps_its_tile(env, monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "1")
    t, box = _run_in_thread({"cmd": "echo early; sleep 30"}, env)
    assert _until(lambda: bool(_fg_rows()))
    fg = _fg_rows()[0]["id"]
    t.join(15)
    res = box["res"]
    assert res.get("id") and not res["id"].startswith("fg-")
    rows = proc_logs.list_for_session(SID)
    assert [r["id"] for r in rows] == [res["id"]]      # listed once
    assert rows[0]["aka"] == fg
    got = proc_logs.read(SID, fg)                      # the old key still reads
    assert got["id"] == res["id"] and "early" in got["streams"][0]["text"]
    assert proc_logs.kill(SID, fg) is True
    assert _until(lambda: not proc_logs.list_for_session(SID)[0]["running"])


def test_a_foreground_command_can_be_stopped_from_the_dock(env):
    t, box = _run_in_thread({"cmd": "sleep 30"}, env)
    assert _until(lambda: bool(_fg_rows()))
    assert proc_logs.kill(SID, _fg_rows()[0]["id"]) is True
    t.join(10)
    assert not t.is_alive()
    assert box["res"]["ok"] is False


def test_no_chat_no_foreground_row(env, monkeypatch):
    monkeypatch.setattr(chat_cancel, "active", lambda: None)
    res = S._t_run_command({"cmd": "echo hi"}, str(env))
    assert res["ok"] is True
    assert proc_logs._FOREGROUND == {} and proc_logs.list_for_session(SID) == []


def test_commands_carry_their_task(env):
    tok = cmd_jobs.set_task({"slug": "t2", "title": "Run the tests"})
    try:
        res = S._t_run_command({"cmd": "sleep 30", "background": True}, str(env))
    finally:
        cmd_jobs.reset_task(tok)
    plain = S._t_run_command({"cmd": "sleep 30", "background": True}, str(env))
    rows = {r["id"]: r for r in proc_logs.list_for_session(SID)}
    assert rows[res["id"]]["task"] == {"slug": "t2", "title": "Run the tests"}
    assert rows[plain["id"]]["task"] is None


def test_a_finished_command_keeps_its_task(env):
    tok = cmd_jobs.set_task({"slug": "t3", "title": "Build"})
    try:
        S._t_run_command({"cmd": "echo done"}, str(env))
    finally:
        cmd_jobs.reset_task(tok)
    rows = proc_logs.list_for_session(SID)
    assert rows and rows[0]["task"]["slug"] == "t3" and not rows[0]["running"]


def test_invoke_tool_scopes_the_task(env):
    seen = []

    def fn(args, cwd):
        seen.append(cmd_jobs._TASK.get())
        return {"ok": True}
    D._invoke_tool(fn, "x", {}, str(env), {"slug": "a", "title": "A"})
    assert seen == [{"slug": "a", "title": "A"}]
    assert cmd_jobs._TASK.get() is None               # reset after the call


def test_running_task_is_the_latest_started():
    class St:
        board = {"a": {"status": "done", "title": "A"},
                 "b": {"status": "running", "title": "B", "_ev_from": 3},
                 "c": {"status": "running", "title": "C", "_ev_from": 9}}
    assert D.running_task(St()) == {"slug": "c", "title": "C"}

    class Empty:
        board = {"a": {"status": "pending"}}
    assert D.running_task(Empty()) is None


def test_live_output_env_keeps_a_user_choice(monkeypatch):
    from aiforge_core.runtime.chat_agent._spool import live_output_env
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    assert live_output_env()["PYTHONUNBUFFERED"] == "1"
    monkeypatch.setenv("PYTHONUNBUFFERED", "")
    assert live_output_env()["PYTHONUNBUFFERED"] == ""
    assert "PATH" in live_output_env() and os.environ.get("PYTHONUNBUFFERED") == ""


def test_a_command_collected_at_the_hand_off_is_listed_once(env, monkeypatch):
    # It exits right at the check-in: look() collects it at the hand-off.
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "1")
    real = S._hand_off

    def exited_first(proc, *a, **kw):
        proc.wait(10)
        return real(proc, *a, **kw)
    monkeypatch.setattr(S, "_hand_off", exited_first)
    t, box = _run_in_thread(
        {"cmd": "echo boom; sleep 1.5", "description": "Quick one"}, env)
    assert _until(lambda: bool(_fg_rows()))
    fg = _fg_rows()[0]["id"]
    t.join(15)
    assert box["res"].get("id") and not box["res"]["id"].startswith("fg-")
    rows = proc_logs.list_for_session(SID)
    assert len(rows) == 1, rows
    row = rows[0]
    assert not row["id"].startswith("fg-") and not row["running"]
    assert row["aka"] == fg and row["label"] == "Quick one"
    got = proc_logs.read(SID, fg)                      # the tile's key still reads
    assert got is not None and "boom" in got["streams"][0]["text"]


def test_two_runs_of_a_reused_pid_get_different_keys(env, monkeypatch):
    class P:
        pid = 777
    class Sp:
        out = err = None
        pgid = 777
        def kill_group(self):
            pass
    a = proc_logs.watch_foreground(P(), Sp(), "x", str(env))
    b = proc_logs.watch_foreground(P(), Sp(), "x", str(env))
    assert a.key != b.key and a.key.startswith("fg-777-")
    proc_logs.drop_foreground(a)
    proc_logs.drop_foreground(b)
    assert proc_logs._FOREGROUND == {}


def test_a_handed_off_command_is_never_listed_twice(env, monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "1")
    seen = []
    t, box = _run_in_thread({"cmd": "echo x; sleep 30"}, env)
    while t.is_alive():
        rows = proc_logs.list_for_session(SID)
        seen.append(len([r for r in rows if r["running"]]))
        time.sleep(0.01)
    t.join(5)
    assert max(seen) == 1


def test_a_nested_call_keeps_the_outer_task(env):
    seen = []

    def fn(args, cwd):
        seen.append(cmd_jobs._TASK.get())
        return {"ok": True}
    tok = cmd_jobs.set_task({"slug": "outer", "title": "O"})
    try:
        D._invoke_tool(fn, "x", {}, str(env))
    finally:
        cmd_jobs.reset_task(tok)
    assert seen == [{"slug": "outer", "title": "O"}]
