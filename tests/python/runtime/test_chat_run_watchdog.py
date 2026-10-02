"""A run that says nothing must say so — and a run whose worker is gone must end.

The loop detectors count agent STEPS; nothing counted seconds. A turn stuck
before its first step (a git snapshot, building context, a model that is down
during the request classifier) showed a timer over nothing, indefinitely. The
run now tracks when it last said anything and what it is doing, the heartbeat
carries both, a quiet run posts a notice at set marks, and a run whose producer
thread has died is closed instead of being reported as running.
"""
from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture
def cr(monkeypatch):
    from aiforge_core.runtime import chat_runs
    monkeypatch.setattr(chat_runs, "_ensure_watchdog", lambda: None)
    monkeypatch.setenv("AIFORGE_CHAT_QUIET_NOTICE_S", "60,180,600")
    yield chat_runs
    chat_runs.finish_all()


def _texts(run):
    return [e.get("text") for e in run.events if e.get("type") == "thought"]


def _check(cr, run, quiet_for):
    cr._check_run(run, run.last_event_at + quiet_for, cr._quiet_marks())


def test_phase_follows_the_events(cr):
    run = cr.start(101)
    cr.set_phase(101, "saving a checkpoint of the workspace")
    assert run.phase == "saving a checkpoint of the workspace"
    run.publish({"type": "tool_start", "name": "run_command"})
    assert run.phase == "running run_command"
    run.publish({"type": "tool", "name": "run_command"})
    assert run.phase == "waiting for the model"
    run.publish({"type": "approval", "id": 1})
    assert run.phase == "waiting for your approval"
    run.publish({"type": "delta", "phase": "answer", "text": "hi"})
    assert run.phase == "the model is writing"


def test_a_quiet_run_says_so_at_each_mark_and_names_what_it_waits_on(cr):
    run = cr.start(102)
    cr.set_phase(102, "waiting for the model to answer")
    _check(cr, run, 30)
    assert _texts(run) == []                          # not quiet long enough
    _check(cr, run, 61)
    _check(cr, run, 90)                               # same mark: not repeated
    assert _texts(run) == ["⏳ No output for 61s — waiting for the model to "
                           "answer. The run is alive; Stop ends it."]
    _check(cr, run, 185)
    _check(cr, run, 601)
    _check(cr, run, 1000)                             # between marks: nothing
    assert len(_texts(run)) == 3
    _check(cr, run, 1021)                             # the last gap (420s) repeats
    assert len(_texts(run)) == 4
    assert "No output for 17m" in _texts(run)[-1]


def test_a_notice_does_not_count_as_activity_but_a_real_event_resets(cr):
    run = cr.start(103)
    before = run.last_event_at
    _check(cr, run, 61)
    assert run.last_event_at == before and run.quiet_notices == 1
    run.publish({"type": "thought", "role": "system", "text": "working"})
    assert run.quiet_notices == 0
    _check(cr, run, 61)
    assert len([t for t in _texts(run) if t.startswith("⏳ No output")]) == 2


def test_notices_can_be_turned_off(cr, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_QUIET_NOTICE_S", "0")
    run = cr.start(104)
    _check(cr, run, 5000)
    assert _texts(run) == []


def test_a_run_whose_worker_died_is_closed_not_left_running(cr):
    run = cr.start(105)
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    run.worker = t                                    # dead, never finished the run
    _check(cr, run, 6)
    assert run.done and not cr.is_running(105)
    kinds = [e["type"] for e in run.events]
    assert kinds[-2:] == ["error", "done"]
    assert "Nothing is running now" in run.events[-2]["text"]


def test_a_live_worker_or_a_handed_off_run_is_left_alone(cr):
    run = cr.start(106)
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    t.start()
    run.worker = t
    _check(cr, run, 6)
    assert not run.done
    stop.set()
    t.join()
    run.worker = None                                 # team driver owns it now
    _check(cr, run, 6)
    assert not run.done


def test_the_heartbeat_carries_silence_and_phase(cr):
    run = cr.start(107)
    cr.set_phase(107, "preparing context (memory, rules, repo map)")
    run.last_event_at = time.time() - 42
    q = run.subscribe()
    it = cr.iter_subscription(run, q, ping_every=0.05)
    ping = next(it)
    assert ping["type"] == "ping" and ping["phase"].startswith("preparing context")
    assert 41 <= ping["quiet_s"] <= 44
    it.close()


def test_a_failed_save_still_closes_the_run(cr, monkeypatch):
    """persist raising used to skip run.finish(): the chat stayed busy and every
    new message got a 409."""
    from aiforge_core.api.routes._chat import _turn_events as te
    run = cr.start(108)
    monkeypatch.setattr(te, "_persist_produce_turn",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    monkeypatch.setattr(te, "_reset_turn_context", lambda ctx: None)
    te._PRODUCE_SEM.acquire()
    te._finalize_produce_turn(108, "/tmp", "p", "answer", [], False, False,
                              {"driver": False, "parallel": False}, "simple",
                              time.time(), None, run, lambda: None)
    assert run.done
    assert any("could not be saved: disk full" in (e.get("text") or "")
               for e in run.events)


def test_checkpoint_git_is_bounded(monkeypatch, tmp_path):
    import subprocess

    from aiforge_core.runtime import checkpoints
    seen = {}

    def _run(cmd, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(subprocess, "run", _run)
    res = checkpoints._git(str(tmp_path), "status")
    assert seen["timeout"] == 120.0 and res.returncode == 124


def test_a_turn_does_not_queue_behind_a_project_sync(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "memory"))
    from aiforge_core.memory import projects
    (tmp_path / "shop").mkdir()
    monkeypatch.setattr(projects, "key_for", lambda p: "shop")
    ent = projects.register(str(tmp_path / "shop"))
    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with projects._REG_LOCK:
            holding.set()
            release.wait(5)

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    holding.wait(2)
    t0 = time.time()
    assert projects.entry(ent["slug"]) is not None          # reads do not lock
    res = projects.sync(ent["slug"], wait_s=0.2)
    assert res.get("skipped") is True and time.time() - t0 < 2
    release.set()
    t.join()
