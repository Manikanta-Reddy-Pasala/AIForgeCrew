"""Review round 2 (runtime): a job the answer calls "still running" really
keeps running, tmux jobs follow the pane's CURRENT process group, no fixed
wall clock on healthy shell work, the fast-role reasoning field is judged per
model, and small job-table fixes."""
from __future__ import annotations

import io
import json
import shutil
import threading
import time
import types
import urllib.error

import pytest

from aiforge_core.runtime import cmd_jobs, cmd_jobs_promote
from aiforge_core.runtime.cmd_signals import job_hint


def _drain(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


# ── 3: an answer while a handed-off job runs promotes it ────────────────────

def test_an_answer_promotes_the_running_job_and_says_so(tmp_path, monkeypatch):
    from aiforge_core.runtime import bg_work
    from aiforge_core.runtime.chat_agent._shell import _t_run_command
    posted = []
    monkeypatch.setattr(bg_work, "_post", lambda sid, text: posted.append(text))
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "0.5")
    turn = cmd_jobs.begin_turn()
    try:
        res = _t_run_command({"cmd": "sleep 2; echo built"}, str(tmp_path))
        assert res.get("running") is True
        jobs = cmd_jobs_promote.promote_turn_jobs()
        assert [j.key for j in jobs] == [res["id"]]
        suffix = cmd_jobs_promote.answer_suffix(jobs)
        assert "Still running in the background" in suffix and res["id"] in suffix
    finally:
        killed = cmd_jobs.end_turn(turn)
    assert killed == 0                          # promoted: the turn's end spares it
    deadline = time.time() + 10
    while not posted and time.time() < deadline:
        time.sleep(0.1)
    assert posted and "finished (exit 0)" in posted[0]


def test_the_running_job_nudge_no_longer_promises_what_end_turn_breaks(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _finish
    job = types.SimpleNamespace(key="bg-3")
    monkeypatch.setattr(cmd_jobs, "turn_running", lambda: [job])
    st = types.SimpleNamespace(convo=[], board_used=False, board={},
                               readonly_mode=False, continue_nudges=0)
    _drain(_finish._final_nudges(st, {"text": "batch 29/40"}, "", False, []))
    text = st.convo[-1]["content"]
    assert "background jobs" in text and "Stop ends" in text


def test_a_pane_job_is_promoted_with_its_own_watcher(monkeypatch):
    from aiforge_core.runtime import bg_work
    posted, state = [], {"alive": True}
    monkeypatch.setattr(bg_work, "_post", lambda sid, text: posted.append(text))
    monkeypatch.setattr(cmd_jobs_promote, "_POLL_S", 0.05)
    job = types.SimpleNamespace(
        key="tmux-9", cmd="make build", explicit=False, owner=None,
        session_id=None, killed=None, streams=[],
        proc=types.SimpleNamespace(returncode=0),
        alive=lambda: state["alive"], kill=lambda why="": None)
    cmd_jobs_promote.promote(job)
    assert job.explicit is True
    state["alive"] = False
    deadline = time.time() + 5
    while not posted and time.time() < deadline:
        time.sleep(0.05)
    assert posted and "make build" in posted[0]


# ── 7: small job-table fixes ────────────────────────────────────────────────

def test_head_streams_so_it_is_not_called_buffering():
    assert "without the pipe" not in job_hint(
        "bg-1", True, None, cmd="./build.sh | head -50")
    assert "without the pipe" in job_hint(
        "bg-1", True, None, cmd="./build.sh | tail -50")


def test_running_does_not_call_alive_under_the_lock(monkeypatch):
    held = []

    class J:
        key, turn, explicit, session_id, owner = "x", None, False, None, None

        def alive(self):
            held.append(cmd_jobs._LOCK.locked())
            return True
    turn = cmd_jobs.begin_turn()
    try:
        j = J()
        j.turn = turn[0]
        with cmd_jobs._LOCK:
            cmd_jobs._JOBS["x"] = j
        assert cmd_jobs.turn_running() == [j]
        assert cmd_jobs.running() == [j]
        assert held and not any(held)
    finally:
        with cmd_jobs._LOCK:
            cmd_jobs._JOBS.pop("x", None)
        cmd_jobs._TURN.reset(turn[1])


def test_a_slot_waiter_cancelled_before_the_slot_aborts_the_resend():
    from aiforge_core.llm import model_wait
    from aiforge_core.runtime.chat_agent._context._generation import _slot_hooks
    sem = threading.Semaphore(0)                # every slot busy
    ev, box = threading.Event(), {"held": False}
    _on_wait, _on_back = _slot_hooks(sem, box, ev, None)
    stop = threading.Event()
    with model_wait.scope(stop, "ticket lost"):
        stop.set()
        _on_back()
    assert ev.is_set() and not box["held"]


# ── 6: no fixed wall clock on the tmux-less path ─────────────────────────────

def test_the_tmux_less_bash_has_no_wall_clock_and_checks_in(monkeypatch, tmp_path):
    from aiforge_core.runtime.tools import bash as B
    monkeypatch.setattr(B, "root", lambda: str(tmp_path))
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "0.5")
    monkeypatch.delenv("AIFORGE_SHELL_TIMEOUT", raising=False)
    turn = cmd_jobs.begin_turn()
    try:
        assert B._fallback_run("echo hi", 0)["stdout"].strip() == "hi"
        t0 = time.monotonic()
        res = B._fallback_run("sleep 30", 0)
        assert time.monotonic() - t0 < 5
        assert res.get("running") is True and res.get("id")
    finally:
        cmd_jobs.end_turn(turn)


# ── 9: the fast-role reasoning field is judged per model ────────────────────

def _refusal(body: bytes, code: int = 400):
    return urllib.error.HTTPError("http://gw/v1/chat/completions", code, "x",
                                  {}, io.BytesIO(body))


@pytest.fixture
def fr():
    from aiforge_core.llm import fast_reasoning
    fast_reasoning.reset()
    yield fast_reasoning
    fast_reasoning.reset()


def test_one_model_rejecting_it_keeps_it_for_the_others(fr):
    assert fr.note_rejection("http://gw/v1", _refusal(
        b'{"error": "Unrecognized request argument: reasoning_effort"}'), "a")
    assert fr.extras_for("http://gw/v1", True, "a") == {}
    assert fr.extras_for("http://gw/v1", True, "b") == {"reasoning_effort": "none"}


@pytest.mark.parametrize("body,code", [
    (b'{"error": "the reasoning model is overloaded"}', 400),
    (b'{"error": "reasoning_effort bad"}', 422),
    (b'{"error": "reasoning_effort bad"}', 404),
])
def test_only_a_400_naming_the_param_is_remembered(fr, body, code):
    assert not fr.note_rejection("http://gw/v1", _refusal(body, code), "a")
    assert fr.extras_for("http://gw/v1", True, "a")


def test_the_resend_drops_a_callers_own_reasoning_effort(fr, monkeypatch):
    from aiforge_core.llm import client as c
    monkeypatch.setattr(c, "_record_usage", lambda *a, **k: None)
    posts = []

    def _post(ep, p, t, **k):
        body = json.loads(p)
        posts.append(body)
        if "reasoning_effort" in body:
            raise _refusal(b'{"error": "unsupported parameter: reasoning_effort"}')
        return {"choices": [{"message": {"content": "DOC"}}]}
    monkeypatch.setattr(c, "_post_with_retry", _post)
    ep = types.SimpleNamespace(model="m", provider="t", extras={},
                               base_url="http://gw/v1")
    out = c._try_post(ep, [{"role": "user", "content": "q"}], temperature=0.0,
                      max_tokens=8, top_p=None,
                      extras={"reasoning_effort": "low"}, timeout_s=5,
                      role="triage", source="primary")
    assert out[0] == "DOC"
    assert len(posts) == 2 and "reasoning_effort" not in posts[1]


# ── 4: tmux jobs follow the pane's CURRENT process group (real tmux) ────────

_needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None,
                                 reason="tmux binary not on PATH")


@pytest.fixture
def real(tmp_path, monkeypatch):
    from aiforge_core.runtime.tools import bash as B
    monkeypatch.setattr(B, "root", lambda: str(tmp_path))
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "1")
    run_id = f"r2-{time.monotonic_ns()}"
    yield B, run_id
    B.destroy_session(run_id)


@_needs_tmux
def test_real_chain_is_followed_to_its_second_command(real):
    from aiforge_core.runtime.chat_agent import _cmd_tools
    B, run_id = real
    res = B.bash("sleep 1.5 && sleep 60", _run_id=run_id)
    assert res["running"] is True
    job = cmd_jobs.find(res["id"])
    first = job.pgid
    time.sleep(2.5)                             # `sleep 1.5` exited
    second = job.pgid
    assert first and second and first != second, (first, second)
    assert job.cpu_active() is True             # a new group is progress
    killed = _cmd_tools._t_command_kill({"id": res["id"]}, "")
    assert killed["running"] is False
    assert B.bash("echo after", _run_id=run_id)["stdout"] == "after"


@_needs_tmux
def test_real_busy_pane_stays_busy_until_the_prompt_is_back(real):
    from aiforge_core.runtime.tools import _tmux_job
    B, run_id = real
    res = B.bash("sleep 60", _run_id=run_id)
    job = cmd_jobs.find(res["id"])
    # A kill that did not take: the pane is still running something.
    real_kill, job._kill = job._kill, (lambda: None)
    job.kill()
    cmd_jobs._forget(job)
    name = job.proc.name
    assert _tmux_job.busy(name) is job          # never typed into
    real_kill()
    deadline = time.time() + 10
    while _tmux_job.busy(name) is not None and time.time() < deadline:
        time.sleep(0.1)
    assert _tmux_job.busy(name) is None
