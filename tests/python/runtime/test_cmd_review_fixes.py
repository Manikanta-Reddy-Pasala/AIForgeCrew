"""Review fixes on the checked-on command loop.

1. A job that FINISHES feeds its whole output and exit code to the
   same-failure / green-run / fewer-failures rules, as its original command;
   partial output of one still running never does.
2. A hand-off (``running: True``) is neither a pass nor a failure.
3. The output-size cap still applies once a command is handed off, even
   while nobody waits on it.
4. "stop the build" / "kill it" kills this chat's handed-off jobs (the steer
   route too); "stop the server" / "stop everything" reaches explicit
   background ones.
5. A job is reachable only from the chat (or run) that started it.
6-8. The turn token survives a prelude error, the Doer guard is locked, and
   act-mode ASK reads survive into the answer.

Real subprocesses for 1, 3, 4 and 5."""
from __future__ import annotations

import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import chat_cancel, cmd_finished, cmd_jobs
from aiforge_core.runtime.chat_agent import _cmd_tools as T
from aiforge_core.runtime.chat_agent import _shell as S
from aiforge_core.runtime.chat_agent._turn import _idle_steps, _outcomes, _progress
from aiforge_core.runtime.doer_no_progress import DoerProgressGuard


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_BG_DB_PATH", str(tmp_path / "bg.db"))
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "1")
    monkeypatch.setenv("AIFORGE_CMD_STUCK_CHECK_S", "45")
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "600")
    monkeypatch.delenv("AIFORGE_CHAT_CMD_TIMEOUT_S", raising=False)
    monkeypatch.setattr(S, "_workspace_root", lambda: None)
    chat_cancel.set_active(None)
    cmd_finished.reset()
    turn = cmd_jobs.begin_turn()
    yield tmp_path
    cmd_jobs.end_turn(turn)
    chat_cancel.set_active(None)
    for job in list(cmd_jobs._JOBS.values()):
        job.kill()
        cmd_jobs._forget(job)


def _run(cmd, cwd, **kw):
    return S._t_run_command({"cmd": cmd, **kw}, str(cwd))


def _finish(res, cwd):
    """Wait a handed-off job out, as the model would."""
    assert res["running"] is True, res
    for _ in range(20):
        res = T._t_command_wait({"id": res["id"], "max_s": 10}, str(cwd))
        if res.get("running") is False:
            return res
    raise AssertionError("job never finished")


def _st(**kw):
    st = SimpleNamespace(stuck_recoveries=0, action_counts={}, reads_new=0,
                         edits_made=0, board={}, **_progress.progress_fields())
    for k, v in kw.items():
        setattr(st, k, v)
    return st


_RED = ("sleep 1.5; echo '___ test_x ___'; "
        "echo 'FAILED tests/a.py::test_x - assert {n} == 4'; "
        "echo '1 failed in 0.{n}s'; exit 1")


# ── 1. a finished job is judged like a finished run ──────────────────────

def test_a_finished_check_in_strikes_the_same_failure(env):
    st = _st()
    verdicts = []
    for i in range(3):
        # A different command each round (a new flag, a new comment) and a
        # different workspace state: the failure is what repeats.
        res = _run(_RED.format(n=i + 1) + f"  # try {i}", env)
        assert _outcomes.note_failure(st, "run_command", {"cmd": "x"}, res) is None
        done = _finish(res, env)
        assert done["ok"] is False and done["code"] == 1
        # The last look may carry none of the FAILED line: it went out early.
        st.state_fp = f"state-{i}"
        verdicts.append(_outcomes.note_failure(
            st, "command_wait", {"id": done["id"]}, done))
    assert verdicts[0] is None and verdicts[1] is None
    assert verdicts[2] is not None and verdicts[2][0] == "nudge"
    assert "test_x" in verdicts[2][1]


def test_partial_output_of_a_running_job_never_strikes(env):
    st = _st()
    running = {"ok": True, "id": "bg-999", "running": True,
               "new_output": "FAILED tests/a.py::test_x - boom\n1 failed"}
    for i in range(5):
        st.state_fp = f"s{i}"
        assert _outcomes.note_failure(st, "command_wait", {"id": "bg-999"},
                                      running) is None
        assert _outcomes.note_failure(st, "run_command", {"cmd": "pytest"},
                                      running) is None
    assert not st.same_fail or not any(st.same_fail.values())


@pytest.fixture
def repo(env):
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    (env / "foo.py").write_text("v1\n")
    runner = env / "pytest"
    runner.write_text("#!/bin/sh\nsleep 1.5\necho '==== 12 passed in 0.40s ===='\n")
    runner.chmod(0o755)
    subprocess.run([*git, "init", "-q"], cwd=env, check=True)
    subprocess.run([*git, "add", "-A"], cwd=env, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=env, check=True)
    return env


def test_a_green_suite_checked_on_to_its_end_is_remembered(repo):
    st = SimpleNamespace(last_green_fp=None)
    res = _run("./pytest -q", repo)
    _outcomes._note_green_tests(st, "run_command", {"cmd": "./pytest -q"}, res,
                                str(repo))
    assert st.last_green_fp is None              # still running proves nothing
    done = _finish(res, repo)
    _outcomes._note_green_tests(st, "command_wait", {"id": done["id"]}, done,
                                str(repo))
    assert st.last_green_fp == _outcomes.content_fingerprint(str(repo))


def test_a_piped_green_checked_on_is_still_not_proof(repo):
    st = SimpleNamespace(last_green_fp="stale")
    done = _finish(_run("./pytest -q | cat", repo), repo)
    _outcomes._note_green_tests(st, "command_wait", {"id": done["id"]}, done,
                                str(repo))
    assert st.last_green_fp == "stale"


def test_a_finished_job_counts_fewer_failures_as_progress(env):
    st = _st()
    two = ("sleep 1.5; echo 'FAILED tests/a.py::test_x'; "
           "echo 'FAILED tests/a.py::test_y'; exit 1")
    one = "sleep 1.5; echo 'FAILED tests/a.py::test_x'; exit 1"
    d2 = _finish(_run(two, env), env)
    _idle_steps._fields(st)
    _idle_steps._signals(st, "command_wait", {"id": d2["id"]}, d2)
    assert st.np_fails == 2
    d1 = _finish(_run(one, env), env)
    assert _idle_steps._signals(st, "command_wait", {"id": d1["id"]}, d1) is True
    assert st.np_fails == 1


def test_the_doer_guard_reads_a_finished_job(env):
    g = DoerProgressGuard()
    run = g._run("inv")
    d = _finish(_run("sleep 1.5; echo 'FAILED tests/a.py::test_x'; exit 1", env), env)
    g._progress(run, "command_wait", {"id": d["id"]}, d)
    assert run["fails"] == 1
    ok = _finish(_run("sleep 1.5; echo '3 passed'", env), env)
    assert g._progress(run, "command_wait", {"id": ok["id"]}, ok) is True
    assert run["fails"] == 0


# ── 2. a hand-off is neither a pass nor a failure ────────────────────────

HANDOFF = {"ok": True, "id": "bg-5", "running": True, "new_output": "",
           "output_growing": False}


def test_a_hand_off_does_not_turn_red_green_in_chat():
    st = _st()
    _idle_steps._fields(st)
    st.np_fails = 3
    assert _idle_steps._signals(st, "run_command", {"cmd": "pytest"}, HANDOFF) is False
    assert st.np_fails == 3
    _idle_steps._signals(st, "run_command", {"cmd": "pytest &"},
                         {"ok": True, "background": True, "id": "bg-6"})
    assert st.np_fails == 3


def test_a_hand_off_does_not_turn_red_green_in_the_doer():
    g = DoerProgressGuard()
    run = g._run("inv")
    run["fails"] = 3
    g._progress(run, "run_shell", {"cmd": "pytest"}, HANDOFF)
    assert run["fails"] == 3


# ── 3. the output cap holds after a hand-off ─────────────────────────────

def test_a_handed_off_job_that_floods_output_is_killed_unwatched(env, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_CMD_OUTPUT_MAX_MB", "0.5")
    res = _run("sleep 1.5; head -c 3000000 /dev/zero | tr '\\0' 'x'; sleep 60", env)
    assert res["running"] is True
    job = cmd_jobs.find(res["id"])
    deadline = time.monotonic() + 15
    while job.alive() and time.monotonic() < deadline:
        time.sleep(0.2)                  # nobody calls command_wait
    assert not job.alive()
    seen = T._t_command_output({"id": res["id"]}, str(env))
    assert seen["running"] is False and seen["ok"] is False
    assert seen.get("stopped") is True and "MB of output" in seen["error"]
    st = _st()
    st.state_fp = "s"
    assert _outcomes.note_failure(st, "command_output", {"id": res["id"]}, seen) is None


def test_the_doer_hand_off_keeps_the_cap(env, monkeypatch):
    from aiforge_core.runtime.doer_tools._shell_run import run_to_completion
    monkeypatch.setenv("AIFORGE_CHAT_CMD_OUTPUT_MAX_MB", "0.5")
    cmd = "sleep 1.5; head -c 3000000 /dev/zero | tr '\\0' 'x'; sleep 60"
    ran = run_to_completion(["bash", "-c", cmd], str(env), 0, 600,
                            checkin_s=1, cmd=cmd)
    job = cmd_jobs.find(ran["job"]["id"])
    deadline = time.monotonic() + 15
    while job.alive() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not job.alive()


# ── 4. a typed stop reaches handed-off jobs ──────────────────────────────

def test_stop_it_kills_handed_off_jobs_not_background_ones(env):
    chat_cancel.set_active(4101)
    handed = _run("echo x; sleep 60", env)
    bg = _run("sleep 60", env, background=True)
    h, b = cmd_jobs.find(handed["id"]), cmd_jobs.find(bg["id"])
    assert cmd_jobs.stop_for_text(4101, "add a stop button") == 0
    assert cmd_jobs.stop_for_text(4101, "how do I kill it?") == 0
    assert cmd_jobs.stop_for_text(4101, "stop the build") == 1
    assert not h.alive() and b.alive()
    seen = T._t_command_output({"id": handed["id"]}, str(env))
    assert seen["stopped"] is True and seen["ok"] is False
    assert cmd_jobs.stop_for_text(4101, "stop the server") == 1
    assert not b.alive()


def test_stop_everything_kills_both(env):
    chat_cancel.set_active(4102)
    h = cmd_jobs.find(_run("echo x; sleep 60", env)["id"])
    b = cmd_jobs.find(_run("sleep 60", env, background=True)["id"])
    assert cmd_jobs.stop_for_text(4102, "stop everything") == 2
    assert not h.alive() and not b.alive()


def test_the_steer_route_kills_now(env):
    from aiforge_core.api.routes._chat import _message as M
    chat_cancel.set_active(4103)
    h = cmd_jobs.find(_run("echo x; sleep 60", env)["id"])
    out = M.chat_session_steer(4103, M._SteerBody(content="kill it"))
    assert not h.alive()
    assert out.get("commands_stopped", 1) == 1


def test_a_wait_on_kill_it_stops_the_job(env, monkeypatch):
    from aiforge_core.runtime import run_interrupt
    chat_cancel.set_active(4104)
    res = _run("echo x; sleep 60", env)
    job = cmd_jobs.find(res["id"])
    monkeypatch.setattr(run_interrupt, "attention", lambda *a, **k: "steer")
    monkeypatch.setattr(run_interrupt, "_newest_queued", lambda sid: "kill it")
    out = T._t_command_wait({"id": res["id"], "max_s": 5}, str(env))
    assert out["steered"] is True and out.get("killed") is True
    assert not job.alive()


def test_a_wait_on_an_extra_detail_leaves_it_running(env, monkeypatch):
    from aiforge_core.runtime import run_interrupt
    chat_cancel.set_active(4105)
    res = _run("echo x; sleep 60", env)
    job = cmd_jobs.find(res["id"])
    monkeypatch.setattr(run_interrupt, "attention", lambda *a, **k: "steer")
    monkeypatch.setattr(run_interrupt, "_newest_queued",
                        lambda sid: "do it in b.py instead")
    out = T._t_command_wait({"id": res["id"], "max_s": 5}, str(env))
    assert out["steered"] is True and not out.get("killed")
    assert job.alive()


# ── 5. jobs belong to the chat that started them ─────────────────────────

def test_another_session_cannot_reach_a_job(env):
    chat_cancel.set_active(5101)
    res = _run("sleep 60", env, background=True)
    assert cmd_jobs.find(res["id"]) is not None
    chat_cancel.set_active(5102)
    tok = cmd_jobs._TURN.set(None)                 # another chat's turn
    try:
        assert cmd_jobs.find(res["id"]) is None
        assert cmd_jobs.find(str(cmd_jobs._JOBS[res["id"]].proc.pid)) is None
        assert res["id"] not in [j.key for j in cmd_jobs.running()]
        err = T._t_command_kill({"id": res["id"]}, str(env))
        assert err["ok"] is False and res["id"] not in err["running"]
        assert cmd_jobs._JOBS[res["id"]].alive()
    finally:
        cmd_jobs._TURN.reset(tok)
    chat_cancel.set_active(5101)
    assert cmd_jobs.find(res["id"]) is not None


def test_a_sessionless_run_sees_only_its_own_turn(env):
    res = _run("echo x; sleep 60", env)            # sessionless, this turn
    assert cmd_jobs.find(res["id"]) is not None
    other = cmd_jobs.begin_turn()
    try:
        assert cmd_jobs.find(res["id"]) is None
    finally:
        cmd_jobs.end_turn(other)


# ── 6. the turn token survives a prelude error ───────────────────────────

def test_a_prelude_error_still_ends_the_turn(tmp_path, monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime.chat_agent import _loop as L
    ended = []
    real_end = cmd_jobs.end_turn
    monkeypatch.setattr(cmd_jobs, "end_turn",
                        lambda t: ended.append(t) or real_end(t))

    def boom(_st):
        raise RuntimeError("prelude")
        yield  # pragma: no cover

    monkeypatch.setattr(L, "_emit_loop_prelude", boom)
    before = cmd_jobs._TURN.get()
    with pytest.raises(RuntimeError):
        list(ca.run_chat_agent([{"role": "user", "content": "hi"}],
                               cwd=str(tmp_path),
                               complete_fn=lambda *_a: "FINAL: ok"))
    assert len(ended) == 1
    assert cmd_jobs._TURN.get() is before


# ── 7. the Doer guard under parallel tools ───────────────────────────────

def test_the_doer_guard_survives_parallel_tool_calls():
    g = DoerProgressGuard()
    errors = []

    def worker(k):
        try:
            for i in range(400):
                g.step(f"run-{(k * 7 + i) % 90}", "read_file", {"path": f"f{i}"},
                       {"ok": True}, {})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(g._runs) <= 64


# ── 8. act-mode ASK reads survive into the answer ────────────────────────

def test_act_mode_ask_reads_reach_the_answer():
    from aiforge_core.runtime.chat_agent._pause import inject, reset, save, take
    reset()
    save(77, [{"role": "user", "content": "OBSERVATION: 1|print(1)"}],
         asked=False, act_ask=True)
    nxt = [{"role": "user", "content": "use the blue one"}]
    assert inject(nxt, take(77)) is False
    assert "1|print(1)" in nxt[0]["content"]
    # A finished plan (no question) still does not leak into a follow-up.
    save(78, [{"role": "user", "content": "OBSERVATION: secret"}], asked=False)
    nxt = [{"role": "user", "content": "now explain b.py"}]
    inject(nxt, take(78))
    assert "secret" not in nxt[0]["content"]
    # An act-mode question answered in a plan-mode turn is not carried.
    save(79, [{"role": "user", "content": "OBSERVATION: x"}], act_ask=True)
    nxt = [{"role": "user", "content": "plan it"}]
    inject(nxt, take(79), plan_mode=True)
    assert "OBSERVATION: x" not in nxt[0]["content"]
    reset()
