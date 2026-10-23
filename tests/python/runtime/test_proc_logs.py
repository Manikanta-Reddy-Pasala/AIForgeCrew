"""The Processes dock: a chat's commands and their live logs.

Real subprocesses: a background printer is listed, tailed by byte offset,
kept after it is collected, stopped from the dock; show_logs opens a running
command's log or follows a file; the API routes serve the same."""
from __future__ import annotations

import time

import pytest

from aiforge_core.runtime import chat_cancel, cmd_jobs, proc_logs
from aiforge_core.runtime.chat_agent import _cmd_tools as T
from aiforge_core.runtime.chat_agent import _shell as S

SID = 4242


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_BG_DB_PATH", str(tmp_path / "bg.db"))
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "1")
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "600")
    monkeypatch.delenv("AIFORGE_CHAT_CMD_TIMEOUT_S", raising=False)
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


def _bg(cmd, cwd, **kw):
    res = S._t_run_command({"cmd": cmd, "background": True, **kw}, str(cwd))
    assert res.get("id"), res
    return res["id"]


def _until(pred, s=10.0):
    end = time.monotonic() + s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_a_background_command_is_listed_with_its_label(env):
    key = _bg("echo hello; sleep 30", env, description="Tail the app log")
    rows = proc_logs.list_for_session(SID)
    assert [r["id"] for r in rows] == [key]
    row = rows[0]
    assert row["running"] is True and row["background"] is True
    assert row["label"] == "Tail the app log"
    assert row["cmd"].startswith("echo hello")
    assert row["cwd"] == str(env)
    assert proc_logs.list_for_session(SID + 1) == []     # other chats see nothing


def test_a_log_is_tailed_by_offset(env):
    key = _bg("echo one; sleep 0.6; echo two; echo oops >&2; sleep 30", env)
    assert _until(lambda: "one" in proc_logs.read(SID, key)["streams"][0]["text"])
    first = proc_logs.read(SID, key)
    offs = [s["next"] for s in first["streams"]]
    assert _until(lambda: "two" in proc_logs.read(SID, key, offs)["streams"][0]["text"])
    nxt = proc_logs.read(SID, key, offs)
    assert "one" not in nxt["streams"][0]["text"]          # only what is new
    assert "oops" in nxt["streams"][1]["text"]
    assert proc_logs.read(SID, "bg-nope") is None
    assert proc_logs.read(SID + 1, key) is None


def test_a_big_log_is_cut_to_the_read_size(env):
    key = _bg("head -c 300000 /dev/zero | tr '\\0' 'x'; sleep 30", env)
    assert _until(lambda: proc_logs.read(SID, key)["bytes"][0] >= 300000)
    r = proc_logs.read(SID, key, [0, 0], max_bytes=4096)
    s = r["streams"][0]
    assert s["skipped"] == 300000 - 4096 and len(s["text"]) == 4096
    assert s["next"] == 300000


def test_reading_does_not_move_the_agents_look(env):
    key = _bg("echo first; sleep 30", env)
    assert _until(lambda: "first" in proc_logs.read(SID, key)["streams"][0]["text"])
    seen = T._t_command_output({"id": key}, str(env))
    assert "first" in seen["new_output"]


def test_an_ended_job_keeps_its_tail_after_it_is_collected(env):
    key = _bg("echo done-line; exit 3", env)
    job = cmd_jobs._JOBS[key]
    assert _until(lambda: not job.alive())
    cmd_jobs.look(job)                                    # collects it
    assert key not in cmd_jobs._JOBS
    rows = proc_logs.list_for_session(SID)
    assert rows[0]["id"] == key and rows[0]["running"] is False
    assert rows[0]["code"] == 3 and rows[0]["ended_at"]
    r = proc_logs.read(SID, key)
    assert "done-line" in r["streams"][0]["text"]
    again = proc_logs.read(SID, key, [s["next"] for s in r["streams"]])
    assert again["streams"][0]["text"] == ""


def test_ended_jobs_are_capped_per_chat(env):
    for i in range(proc_logs.ENDED_PER_SESSION + 3):
        key = _bg(f"echo {i}", env)
        job = cmd_jobs._JOBS[key]
        assert _until(lambda: not job.alive())
        cmd_jobs.look(job)
    assert len(proc_logs.list_for_session(SID)) == proc_logs.ENDED_PER_SESSION
    proc_logs.forget_session(SID)
    assert proc_logs.list_for_session(SID) == []


def test_the_dock_stops_a_command(env):
    key = _bg("sleep 30", env)
    assert proc_logs.kill(SID, key) is True
    job = cmd_jobs._JOBS.get(key)
    assert job is None or not job.alive()
    assert proc_logs.kill(SID, key) is False
    assert proc_logs.kill(SID + 1, "bg-1") is False


def test_show_logs_by_id_marks_it_shown(env):
    key = _bg("sleep 30", env)
    res = T._t_show_logs({"id": key}, str(env))
    assert res["ok"] and res["id"] == key and "Processes panel" in res["note"]
    assert proc_logs.list_for_session(SID)[0]["shown"] > 0
    bad = T._t_show_logs({"id": "bg-999999"}, str(env))
    assert bad["ok"] is False


def test_show_logs_without_id_shows_every_running_command(env):
    a, b = _bg("sleep 30", env), _bg("sleep 30", env)
    res = T._t_show_logs({}, str(env))
    assert set(res["shown"]) == {a, b}


def test_show_logs_with_nothing_running_says_so(env):
    res = T._t_show_logs({}, str(env))
    assert res["ok"] is False and "run_command" in res["hint"]


def test_show_logs_follows_a_file(env):
    log = env / "app.log"
    log.write_text("".join(f"old {i}\n" for i in range(10)))
    res = T._t_show_logs({"path": "app.log", "lines": 3}, str(env))
    assert res["ok"] and res["path"] == str(log)
    key = res["id"]
    row = proc_logs.list_for_session(SID)[0]
    assert row["label"] == "log: app.log" and row["shown"] > 0
    assert _until(lambda: "old 9" in proc_logs.read(SID, key)["streams"][0]["text"])
    text = proc_logs.read(SID, key)["streams"][0]["text"]
    assert "old 6" not in text                            # last 3 lines only
    with log.open("a") as f:
        f.write("new line\n")
    assert _until(lambda: "new line" in proc_logs.read(SID, key)["streams"][0]["text"])


def test_show_logs_reuses_a_running_tail_of_the_same_file(env):
    (env / "app.log").write_text("x\n")
    first = T._t_show_logs({"path": "app.log"}, str(env))
    again = T._t_show_logs({"path": str(env / "app.log")}, str(env))
    assert again["id"] == first["id"] and again["reused"] is True
    assert len(proc_logs.list_for_session(SID)) == 1


def test_show_logs_refuses_a_missing_file(env):
    res = T._t_show_logs({"path": "nope.log"}, str(env))
    assert res["ok"] is False and "no such file" in res["error"]


def test_show_logs_needs_a_chat(env, monkeypatch):
    monkeypatch.setattr(chat_cancel, "active", lambda: None)
    assert T._t_show_logs({}, str(env))["ok"] is False


def test_show_logs_is_a_core_read_only_tool():
    from aiforge_core.runtime.chat_agent._registry import TOOLS
    from aiforge_core.runtime.chat_agent._tools import _schemas
    from aiforge_core.runtime.tools import tool_policy
    assert "show_logs" in TOOLS
    assert "show_logs" in _schemas.NATIVE_TOOL_NAMES
    assert "show_logs" in _schemas._CORE_ACT
    assert "show_logs" in tool_policy._READONLY_ALWAYS_ALLOW


# ── the API ──────────────────────────────────────────────────────────────


@pytest.fixture
def client(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aiforge_core.api.routes._chat import _processes
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "get_session",
                        lambda sid: {"id": sid} if sid == SID else None)
    app = FastAPI()
    app.include_router(_processes.router)
    return TestClient(app)


def test_api_lists_reads_and_kills(client, env):
    key = _bg("echo via-api; sleep 30", env)
    rows = client.get(f"/api/chat/sessions/{SID}/processes").json()["processes"]
    assert rows[0]["id"] == key
    assert _until(lambda: "via-api" in client.get(
        f"/api/chat/sessions/{SID}/processes/{key}/log").json()["streams"][0]["text"])
    r = client.get(f"/api/chat/sessions/{SID}/processes/{key}/log",
                   params={"offsets": "0,0", "max_bytes": 2048}).json()
    assert r["streams"][0]["start"] == 0
    assert client.get(f"/api/chat/sessions/{SID}/processes/{key}/log",
                      params={"offsets": "x"}).status_code == 400
    assert client.post(f"/api/chat/sessions/{SID}/processes/{key}/kill").json()["ok"]
    assert client.post(f"/api/chat/sessions/{SID}/processes/{key}/kill").status_code == 404


def test_api_404s(client):
    assert client.get("/api/chat/sessions/1/processes").status_code == 404
    assert client.get(f"/api/chat/sessions/{SID}/processes/bg-0/log").status_code == 404
