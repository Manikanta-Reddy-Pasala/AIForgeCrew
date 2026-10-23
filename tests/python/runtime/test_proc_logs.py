"""The Processes dock: a chat's commands and their live logs.

Real subprocesses: a background printer is listed, tailed by byte offset,
kept after it is collected, stopped from the dock; show_logs opens a running
command's log or follows a file; the API routes serve the same."""
from __future__ import annotations

import os
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
    assert "show_logs" not in _schemas._CORE_ACT      # added by cue
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


# ── review fixes ─────────────────────────────────────────────────────────


def test_a_running_commands_partial_line_waits_for_its_newline(env):
    key = _bg("printf 'whole\\nhal'; sleep 1; printf 'f done\\n'; sleep 30", env)
    assert _until(lambda: "whole" in proc_logs.read(SID, key)["streams"][0]["text"])
    r = proc_logs.read(SID, key, [0, 0])
    assert r["streams"][0]["text"] == "whole\n"
    assert r["streams"][0]["next"] == len("whole\n")
    offs = [s["next"] for s in r["streams"]]
    assert _until(lambda: proc_logs.read(SID, key, offs)["streams"][0]["text"] == "half done\n")


def test_show_logs_on_an_ended_job_reaches_the_list(env):
    key = _bg("echo bye", env)
    job = cmd_jobs._JOBS[key]
    assert _until(lambda: not job.alive())
    cmd_jobs.look(job)
    assert T._t_show_logs({"id": key}, str(env))["ok"]
    assert proc_logs.list_for_session(SID)[0]["shown"] > 0


def test_shown_is_per_chat(env):
    key = _bg("sleep 30", env)
    proc_logs.mark_shown(SID, key)
    assert proc_logs._shown(SID, key) > 0
    assert proc_logs._shown(SID + 1, key) == 0


def test_shown_keeps_rising(env):
    a, b = _bg("sleep 30", env), _bg("sleep 30", env)
    proc_logs.mark_shown(SID, a)
    proc_logs.mark_shown(SID, b)
    assert proc_logs._shown(SID, b) > proc_logs._shown(SID, a) > 10 ** 15
    assert proc_logs._shown(SID, b) < 2 ** 53          # exact in a JS number


def test_deleting_a_chat_stops_its_tails_and_keeps_nothing(env):
    (env / "app.log").write_text("x\n")
    tail = T._t_show_logs({"path": "app.log"}, str(env))["id"]
    other = _bg("sleep 30", env)
    proc_logs.forget_session(SID)
    job = cmd_jobs._JOBS.get(tail)
    assert job is None or not job.alive()
    assert cmd_jobs._JOBS[other].alive()              # other commands untouched
    if tail in cmd_jobs._JOBS:
        cmd_jobs._forget(cmd_jobs._JOBS[tail])     # its end keeps nothing
    assert [r["id"] for r in proc_logs.list_for_session(SID)] == [other]


def test_show_logs_path_stays_inside_the_workspace(env, monkeypatch, tmp_path):
    from pathlib import Path
    ws = env / "ws"
    ws.mkdir()
    (ws / "in.log").write_text("ok\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("s\n")
    monkeypatch.setattr(S, "_workspace_root", lambda: Path(ws).resolve())
    bad = T._t_show_logs({"path": str(outside)}, str(ws))
    assert bad["ok"] is False and "escapes" in bad["error"]
    assert T._t_show_logs({"path": "in.log"}, str(ws))["ok"]


def test_a_follow_tail_has_a_deadline(env, monkeypatch):
    (env / "app.log").write_text("x\n")
    key = T._t_show_logs({"path": "app.log"}, str(env))["id"]
    job = cmd_jobs._JOBS[key]
    assert job.follow_path == str(env / "app.log")
    assert job.watch_opts["deadline"] > time.monotonic() + 60


def test_a_job_is_kept_before_it_leaves_the_table(env, monkeypatch):
    key = _bg("echo x", env)
    job = cmd_jobs._JOBS[key]
    assert _until(lambda: not job.alive())
    seen = {}
    real = proc_logs.remember

    def spy(j):
        seen["in_table"] = j.key in cmd_jobs._JOBS
        real(j)
    monkeypatch.setattr(proc_logs, "remember", spy)
    cmd_jobs._forget(job)
    assert seen["in_table"] is True and key not in cmd_jobs._JOBS


@pytest.mark.parametrize("text", [
    "show me the logs of the api", "tail the build log",
    "Show me the output of the heartbeat process", "follow app.log"])
def test_show_logs_is_added_when_the_user_asks_for_logs(text):
    from aiforge_core.runtime.chat_agent._tools import _families
    assert "logs" in _families.named_families(text)


def test_show_logs_is_not_added_for_unrelated_text():
    from aiforge_core.runtime.chat_agent._tools import _families
    assert "logs" not in _families.named_families("what is 2+2")


# ── log files, whole-log view and download ───────────────────────────────


def test_a_redirect_target_is_a_log_file(env):
    key = _bg("while true; do echo tick; sleep 0.2; done > run.log 2>&1", env)
    assert _until(lambda: (env / "run.log").exists())
    files = proc_logs.list_for_session(SID)[0]["log_files"]
    assert files == [str((env / "run.log").resolve())]
    assert _until(lambda: "tick" in (proc_logs.read_file(SID, key, files[0]) or {}).get("text", ""))


def test_a_file_the_process_keeps_open_is_found(env):
    script = env / "w.py"
    script.write_text("import time\nf = open('server.out', 'a')\n"
                      "while True:\n    f.write('up\\n'); f.flush(); time.sleep(0.2)\n")
    key = _bg(f"python3 {script}", env)
    want = str((env / "server.out").resolve())
    assert _until(lambda: want in proc_logs.list_for_session(SID)[0]["log_files"], s=15)
    r = proc_logs.read_file(SID, key, want, 0)
    assert r["text"].startswith("up\n") and r["running"] is True


def test_only_listed_files_can_be_read(env, tmp_path):
    key = _bg("sleep 30 > a.log", env)
    other = tmp_path / "secret.txt"
    other.write_text("s\n")
    assert proc_logs.read_file(SID, key, str(other)) is None
    assert proc_logs.read_file(SID + 1, key, str(env / "a.log")) is None
    assert proc_logs.read_file(SID, key, str(env / "a.log"), 0)["path"] == str((env / "a.log").resolve())


def test_an_ended_job_keeps_its_log_files(env):
    key = _bg("echo hi > done.log", env)
    job = cmd_jobs._JOBS[key]
    assert _until(lambda: not job.alive())
    cmd_jobs.look(job)
    row = proc_logs.list_for_session(SID)[0]
    assert row["log_files"] == [str((env / "done.log").resolve())]
    assert proc_logs.read_file(SID, key, row["log_files"][0], 0)["text"] == "hi\n"


def test_download_is_the_whole_output(env):
    key = _bg("seq 1 50000; echo bad >&2; sleep 30", env)
    assert _until(lambda: proc_logs.read(SID, key)["bytes"][1] > 0)
    data = b"".join(proc_logs.output_chunks(SID, key, chunk=4096)).decode()
    assert data.startswith("1\n2\n") and "\n50000\n" in data
    assert data.rstrip().endswith("----- stderr -----\nbad")
    assert proc_logs.output_chunks(SID + 1, key) is None


def test_a_prompt_without_newline_shows_once_output_goes_quiet(env, monkeypatch):
    monkeypatch.setattr(proc_logs, "_PARTIAL_QUIET_S", 0.3)
    key = _bg("printf 'Password: '; sleep 30", env)
    assert _until(lambda: proc_logs.read(SID, key, [0, 0])["streams"][0]["text"] == "Password: ")


def test_remember_skips_a_job_already_closed(env):
    key = _bg("echo x", env)
    job = cmd_jobs._JOBS[key]
    assert _until(lambda: not job.alive())
    cmd_jobs.look(job)
    good = proc_logs.read(SID, key)["streams"][0]["text"]
    proc_logs.remember(job)                      # a late second collector
    assert proc_logs.read(SID, key)["streams"][0]["text"] == good == "x\n"


def test_api_file_and_download(client, env):
    key = _bg("while true; do echo line; sleep 0.2; done > api.log", env)
    path = str((env / "api.log").resolve())
    assert _until(lambda: (env / "api.log").stat().st_size > 0 if (env / "api.log").exists() else False)
    assert _until(lambda: path in client.get(
        f"/api/chat/sessions/{SID}/processes").json()["processes"][0]["log_files"])
    r = client.get(f"/api/chat/sessions/{SID}/processes/{key}/file",
                   params={"path": path, "offset": 0})
    assert r.status_code == 200 and r.json()["text"].startswith("line\n")
    assert client.get(f"/api/chat/sessions/{SID}/processes/{key}/file",
                      params={"path": "/etc/passwd"}).status_code == 404
    d = client.get(f"/api/chat/sessions/{SID}/processes/{key}/download", params={"path": path})
    assert d.status_code == 200 and d.text.startswith("line\n")
    assert "attachment" in d.headers["content-disposition"]
    out = client.get(f"/api/chat/sessions/{SID}/processes/{key}/download")
    assert out.status_code == 200 and "attachment" in out.headers["content-disposition"]


def test_a_quoted_gt_is_not_a_redirect(env):
    from aiforge_core.runtime import proc_log_files as F
    assert F._from_cmd("grep '>' notes.txt", str(env)) == []
    assert F._from_cmd("run > out.txt 2>> err.txt", str(env)) == [
        str(env / "out.txt"), str(env / "err.txt")]
    assert F._from_cmd("cmd &> all.log", str(env)) == [str(env / "all.log")]
    assert F._from_cmd("cmd > 'my run.txt'", str(env)) == [str(env / "my run.txt")]


def test_files_outside_the_folder_must_be_log_named_and_not_hidden(env, tmp_path, monkeypatch):
    from aiforge_core.runtime import proc_log_files as F
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    key_file = home / ".ssh" / "id_rsa"
    key_file.write_text("k\n")
    app_log = home / "app.log"
    app_log.write_text("a\n")
    hidden_log = home / ".ssh" / "x.log"
    hidden_log.write_text("h\n")
    monkeypatch.setattr(F, "_roots", lambda job: ["/nonexistent-root"])
    key = _bg(f"sleep 30; echo >> {key_file}; echo > {app_log}; echo > {hidden_log}", env)
    files = proc_logs.list_for_session(SID)[0]["log_files"]
    assert str(app_log.resolve()) in files
    assert str(key_file.resolve()) not in files and str(hidden_log.resolve()) not in files
    assert proc_logs.read_file(SID, key, str(key_file)) is None


def test_a_listed_file_swapped_for_a_symlink_is_not_read(env, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("s\n")
    key = _bg("sleep 30 > swap.log", env)
    assert proc_logs.list_for_session(SID)[0]["log_files"]
    log = env / "swap.log"
    log.unlink()
    log.symlink_to(secret)
    assert proc_logs.read_file(SID, key, str(log), 0) is None


def test_downloading_a_growing_file_stops_at_its_size_then(env):
    key = _bg("while true; do echo grow; done > big.log", env)
    path = str((env / "big.log").resolve())
    assert _until(lambda: (env / "big.log").stat().st_size > 100000)
    opened = proc_logs.open_file(SID, key, path)
    assert opened is not None
    fd = opened[1].fileno()
    before = os.fstat(fd).st_size
    gen = proc_logs.file_chunks(opened[1], chunk=8192)
    first = next(gen)                      # the size is fixed on the first read
    fixed = os.fstat(fd).st_size
    data = first + b"".join(gen)
    assert before <= len(data) <= fixed
    assert os.path.getsize(path) > len(data)   # it kept growing; we did not follow
