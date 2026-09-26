"""The pipeline Doer checks on its commands too: run_shell hands back a
command still running at the check-in (or one that printed an error or a
prompt), command_wait / command_output / command_kill are Doer tools, the
repeat guard keys a wait on the job's progress, and a run's commands die with
the run — and with the ticket claim, including one a crashed attempt left.

Real subprocesses throughout."""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import bg_work, cmd_jobs
from aiforge_core.runtime.doer_tools import _fs, _tools


@pytest.fixture
def repo(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("AIFORGE_BG_DB_PATH", str(tmp_path / "bg.db"))
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "1")
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "600")
    monkeypatch.delenv("AIFORGE_SHELL_TIMEOUT", raising=False)
    from aiforge_core.runtime import request_context
    monkeypatch.setattr(request_context, "get_repo_root", lambda: str(tmp_path))
    from aiforge_core.runtime.tools import command_risk
    monkeypatch.setattr(command_risk, "assess", lambda cmd: {"level": "safe"})
    turn = cmd_jobs.begin_turn()
    yield tmp_path
    cmd_jobs.end_turn(turn)


def test_a_quick_command_is_unchanged(repo):
    out = _fs.run_shell("echo hi")
    assert out["ok"] is True and out["returncode"] == 0
    assert out["stdout"] == "hi\n"


def test_a_long_command_comes_back_running_and_can_be_waited_on(repo):
    t0 = time.monotonic()
    out = _fs.run_shell("for i in 1 2 3 4 5 6; do echo step $i; sleep 0.4; done")
    assert time.monotonic() - t0 < 3
    assert out["running"] is True and "step 1" in out["new_output"]
    done = _tools.command_wait(out["id"], 20)
    assert done["running"] is False and done["code"] == 0
    assert "step 6" in done["new_output"]


def test_an_error_while_running_comes_back_at_once(repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "30")
    t0 = time.monotonic()
    out = _fs.run_shell("sleep 0.5; echo '[ERROR] COMPILATION ERROR'; sleep 30")
    assert time.monotonic() - t0 < 6
    assert out["running"] is True and "ERROR" in out["returned_because"]
    peek = _tools.command_output(out["id"])
    assert peek["running"] is True
    killed = _tools.command_kill(out["id"])
    assert killed["killed"] is True and killed["running"] is False


def test_the_doer_is_given_the_three_tools():
    pytest.importorskip("google.adk")
    from aiforge_core.runtime import doer_tools
    names = {getattr(t, "name", "") for t in doer_tools.adk_function_tools("doer")}
    assert {"command_wait", "command_output", "command_kill", "run_shell"} <= names


def test_the_doer_prompt_teaches_them():
    from aiforge_core.runtime.prompts import doer
    text = " ".join(str(v) for v in vars(doer).values() if isinstance(v, str))
    assert "command_wait" in text and "command_kill" in text


# ── the repeat guard ────────────────────────────────────────────────────

def _guard_calls(cb, job_id, n, gap):
    ctx = SimpleNamespace(state={})
    tool = SimpleNamespace(name="command_wait")
    res = None
    for _ in range(n):
        res = cb(tool=tool, args={"id": job_id}, tool_context=ctx)
        if res is not None:
            return res
        time.sleep(gap)
    return res


def test_waiting_on_a_working_job_is_not_a_repeat(repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_TOOL_REPEAT_LIMIT", "3")
    from aiforge_core.runtime.repeat_guard import make_repeat_guard_callback
    out = _fs.run_shell("while true; do echo tick; sleep 0.1; done")
    assert _guard_calls(make_repeat_guard_callback(), out["id"], 6, 0.3) is None
    _tools.command_kill(out["id"])


def test_waiting_on_a_job_doing_nothing_is(repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_TOOL_REPEAT_LIMIT", "3")
    from aiforge_core.runtime.repeat_guard import make_repeat_guard_callback
    out = _fs.run_shell("echo once; sleep 30")
    time.sleep(0.3)
    blocked = _guard_calls(make_repeat_guard_callback(), out["id"], 6, 0.05)
    assert blocked is not None and blocked["error"] == "repeated_call"
    _tools.command_kill(out["id"])


# ── cleanup: run end, claim end, reclaim ────────────────────────────────

class _SessionSvc:
    async def get_session(self, **_kw):
        return SimpleNamespace(state={"done": True})


class _Runner:
    def __init__(self, box, error=None):
        self.box, self.error = box, error

    async def run_async(self, **_kw):
        self.box["job"] = _fs.run_shell("echo started; sleep 30")
        yield SimpleNamespace()
        if self.error:
            raise self.error


@pytest.mark.parametrize("error", [None, RuntimeError("boom")])
def test_a_pipeline_run_takes_its_commands_with_it(repo, monkeypatch, error):
    from aiforge_core.runtime.adk_runner import _pipeline as pl
    monkeypatch.setattr(pl, "_pipeline_deadline_s", lambda: 0)
    box: dict = {}
    asyncio.run(pl._drive_pipeline(_Runner(box, error), _SessionSvc(), "s",
                                   SimpleNamespace()))
    assert box["job"]["running"] is True
    assert cmd_jobs.find(box["job"]["id"]) is None      # killed + collected


def test_the_claim_ending_stops_its_commands(repo):
    from aiforge_core.tickets import lease
    with lease.hold_claim(424242, interval_s=60):
        out = _fs.run_shell("echo started; sleep 30")
        job = cmd_jobs.find(out["id"])
        assert job.owner == "ticket-424242"
    assert not job.alive()


def test_a_reclaim_stops_what_a_crashed_attempt_left(repo):
    """A row saved by an earlier process (its job table is gone) whose
    process still runs is stopped when the ticket is claimed again."""
    from aiforge_core.tickets import lease
    orphan = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        bg_work._insert(None, "command", str(repo),
                        {"cmd": "sleep 30", "owner": "ticket-515151"},
                        pid=orphan.pid, pgid=os.getpgid(orphan.pid))
        with lease.hold_claim(515151, interval_s=60):
            deadline = time.monotonic() + 5
            while orphan.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            assert orphan.poll() is not None
    finally:
        if orphan.poll() is None:
            orphan.kill()
        orphan.wait()
