"""Final-review runtime fixes: the PR reviewer's read timeout is a stall (the
bound doubles, the resend waits for the busy server), promoted pane jobs are
watched for prompts / hangs and forgotten, and the test-gaming baseline is
hashes kept per run — never evicted under a live run, never replaced by HEAD.
"""
from __future__ import annotations

import subprocess
import threading
import time
import types

import pytest

from aiforge_core.llm import endpoint_breaker, model_wait, request_health
from aiforge_core.runtime import cmd_jobs, cmd_jobs_promote, gaming_changes
from aiforge_core.runtime import gaming_check as TG
from aiforge_core.runtime import pr_reviewer

# ── 3: pr_reviewer ─────────────────────────────────────────────────────────


class Timeout(Exception):
    """Named like litellm's read timeout."""


@pytest.fixture
def _fast_wait(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_WAIT_MAX_S", "0")
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "5")
    monkeypatch.setenv("AIFORGE_LLM_STREAM_IDLE_S", "0")

    def _gaps(cap=None):
        while True:
            yield 0.02
    monkeypatch.setattr(model_wait, "delays", _gaps)
    model_wait._reset_for_tests()
    endpoint_breaker.reset()
    yield
    model_wait._reset_for_tests()


def _review_with(monkeypatch, sends: list, fail_first: int):
    ep = {"model": "openai/m", "api_base": "http://m/v1", "api_key": "k"}

    def completion(**kw):
        sends.append(("send", kw["timeout"]))
        if len([s for s in sends if s[0] == "send"]) <= fail_first:
            raise Timeout("Request timed out")
        return {"choices": [{"message": {"content": '{"ok": true}'}}]}
    litellm = types.SimpleNamespace(completion=completion)
    send = pr_reviewer._sender(litellm, ep, [{"role": "user", "content": "x"}])
    return model_wait.call_with_wait(send, url=ep["api_base"],
                                     api_key=ep["api_key"], model=ep["model"])


def test_a_review_read_timeout_doubles_the_bound(monkeypatch, _fast_wait):
    monkeypatch.setattr(model_wait, "live_probe", lambda *a, **k: True)
    sends: list = []
    assert _review_with(monkeypatch, sends, fail_first=2) == '{"ok": true}'
    bounds = [b for kind, b in sends if kind == "send"]
    assert bounds == [5.0, 10.0, 20.0]


def test_a_resend_waits_while_the_server_still_works_on_the_old_one(
        monkeypatch, _fast_wait):
    """The abandoned review keeps the server busy: the probe queues (not
    answered promptly) → no resend until a probe is answered."""
    sends: list = []
    busy = {"n": 3}

    def up(*a, **k):
        busy["n"] -= 1
        sends.append(("probe", busy["n"] <= 0))
        return busy["n"] <= 0
    monkeypatch.setattr(model_wait, "live_probe", lambda *a, **k: False)
    monkeypatch.setattr(model_wait, "probe", up)
    _review_with(monkeypatch, sends, fail_first=1)
    kinds = [k for k, _ in sends]
    second = [i for i, k in enumerate(kinds) if k == "send"][1]
    assert sends[second - 1] == ("probe", True)       # answered, then resent
    assert [b for k, b in sends if k == "send"] == [5.0, 10.0]


def test_a_non_timeout_error_is_not_a_stall():
    exc = ValueError("bad json")
    assert pr_reviewer._as_stall(exc, {"api_base": ""}, []) is None


# ── 4: promoted pane jobs ─────────────────────────────────────────────────


def _pane_job(key, *, session_id=7, tail="", alive=None, kills=None):
    state = {"alive": True}
    kills = kills if kills is not None else []

    def kill(why=""):
        kills.append(why)
        job.killed = why
        state["alive"] = False
    job = types.SimpleNamespace(
        key=key, cmd="apt install foo", explicit=False, owner=None,
        session_id=session_id, killed=None, streams=[], pgid=None,
        proc=types.SimpleNamespace(returncode=None),
        alive=alive or (lambda: state["alive"]), kill=kill,
        size=lambda: 10, _tail=lambda: tail, close=lambda: None)
    return job, state, kills


def _wait_for(pred, limit=10.0):
    end = time.time() + limit
    while not pred() and time.time() < end:
        time.sleep(0.05)
    return pred()


def test_a_promoted_job_at_a_prompt_is_announced_then_killed_as_hung(
        monkeypatch):
    from aiforge_core.runtime import bg_work
    posted = []
    monkeypatch.setattr(bg_work, "_post", lambda sid, text: posted.append(text))
    monkeypatch.setattr(cmd_jobs_promote, "_POLL_S", 0.05)
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "0.5")
    job, state, kills = _pane_job("tmux-41", tail="Do you want to continue? [y/N] ")
    with cmd_jobs._LOCK:
        cmd_jobs._JOBS[job.key] = job
    assert cmd_jobs_promote.promote(job) is True
    assert _wait_for(lambda: len(posted) >= 2)
    assert "tmux-41 is waiting for input" in posted[0] and "[y/N]" in posted[0]
    assert kills and "HUNG" in kills[0]
    assert "stopped" in posted[-1]
    assert _wait_for(lambda: job.key not in cmd_jobs._JOBS)   # forgotten


def test_a_promoted_job_that_ends_is_forgotten(monkeypatch):
    from aiforge_core.runtime import bg_work
    posted = []
    monkeypatch.setattr(bg_work, "_post", lambda sid, text: posted.append(text))
    monkeypatch.setattr(cmd_jobs_promote, "_POLL_S", 0.05)
    job, state, _ = _pane_job("tmux-42")
    job.proc.returncode = 0
    with cmd_jobs._LOCK:
        cmd_jobs._JOBS[job.key] = job
    cmd_jobs_promote.promote(job)
    state["alive"] = False
    assert _wait_for(lambda: job.key not in cmd_jobs._JOBS)
    assert posted and "finished (exit 0)" in posted[-1]


def test_a_sessionless_job_is_not_promoted(monkeypatch):
    job, _, _ = _pane_job("tmux-43", session_id=None)
    monkeypatch.setattr(cmd_jobs, "turn_running", lambda: [job])
    assert cmd_jobs_promote.promote(job) is False
    assert job.explicit is False                 # end_turn still kills it
    assert cmd_jobs_promote.promote_turn_jobs() == []


# ── 5: the test-gaming baseline ───────────────────────────────────────────

THE_LIVE_ONE = (
    "import os\n\ndef fmt(x):\n    if os.environ.get('PYTEST_CURRENT_TEST'):\n"
    "        return '2.50'\n    return str(x)\n")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    gaming_changes._reset_for_tests()
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "money.py").write_text("def fmt(x):\n    return str(x)\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_money.py").write_text(
        "from money import fmt\n\ndef test_a():\n    assert fmt(2.5) == '2.5'\n\n"
        "def test_b():\n    assert fmt(2.5) == '2.50'\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "money.py").write_text(THE_LIVE_ONE)       # the user's WIP
    yield tmp_path
    gaming_changes._reset_for_tests()


def test_a_long_runs_baseline_is_not_evicted_by_later_ones(repo):
    base = TG.baseline(str(repo))
    for _ in range(100):                     # many turns / runs meanwhile
        gaming_changes.release(TG.baseline(str(repo)))
        TG.baseline(str(repo))
    assert TG.check(str(repo), base) == []   # the WIP is still the user's
    assert TG.check(str(repo))               # (vs HEAD it would be flagged)


def test_a_missing_or_released_baseline_skips_the_check(repo):
    assert TG.check(str(repo), "unavailable") == []
    base = TG.baseline(str(repo))
    gaming_changes.release(base)
    assert TG.check(str(repo), base) == []


def test_an_incomplete_baseline_skips_the_check(repo, monkeypatch):
    monkeypatch.setattr(gaming_changes, "_MAX_DIRTY", 0)
    base = TG.baseline(str(repo))
    assert base
    assert TG.check(str(repo), base) == []


def test_the_baseline_keeps_hashes_not_content(repo):
    base = TG.baseline(str(repo))
    stored = gaming_changes._BASES[base]["files"]["money.py"]
    assert isinstance(stored, bytes) and len(stored) % 8 == 0
    assert b"PYTEST_CURRENT_TEST" not in stored and b"fmt" not in stored


def test_the_runs_own_line_in_a_dirty_file_is_still_found(repo):
    base = TG.baseline(str(repo))
    (repo / "money.py").write_text(THE_LIVE_ONE + "\nT = 1\n")
    changes = gaming_changes.repo_changes(str(repo), base)
    assert changes == {"money.py": {7, 8}}      # the blank line and T


def test_an_unchanged_file_is_hashed_once(repo, monkeypatch):
    reads = []
    real = gaming_changes._read
    monkeypatch.setattr(gaming_changes, "_read",
                        lambda root, rel: reads.append(rel) or real(root, rel))
    TG.baseline(str(repo))
    TG.baseline(str(repo))
    assert reads.count("money.py") == 1


def test_the_baseline_is_ready_when_it_returns(repo, monkeypatch):
    started = []
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: started.append(1))
    base = TG.baseline(str(repo), background=True)
    assert not started and gaming_changes._BASES[base]["complete"]


def test_runs_release_their_baseline(repo, monkeypatch):
    from aiforge_core.runtime.adk_runner import _pipeline, _run_inputs
    from aiforge_core.runtime.graph_pipeline import _scope
    monkeypatch.setattr(_scope, "_repo_root_for_scope", lambda: str(repo))
    state = _run_inputs.seed_gaming_base({})
    assert state["gaming_base"] in gaming_changes._BASES
    _pipeline._release_gaming_base(state)
    assert state["gaming_base"] not in gaming_changes._BASES


def test_a_failed_baseline_is_a_skip_not_head(repo, monkeypatch):
    from aiforge_core.runtime.adk_runner import _run_inputs

    def boom(*a, **k):
        raise RuntimeError("git broke")
    monkeypatch.setattr(gaming_changes, "baseline", boom)
    state = _run_inputs.seed_gaming_base({})
    assert state["gaming_base"] == "unavailable"
    assert TG.check(str(repo), state["gaming_base"]) == []
