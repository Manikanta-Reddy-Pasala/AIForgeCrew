"""The eight scheduler gaps: report-back, agent schedules, background
commands, steer/stop, webhooks, concurrent watches, restart resume, and
hook output the model can see."""
from __future__ import annotations

import os
import threading
import time

import pytest

from aiforge_core.jobs import scheduler, store
from aiforge_core.runtime import bg_work, hooks
from aiforge_core.runtime.chat_agent._tools import _watch


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return bool(cond())


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_BG_DB_PATH", str(tmp_path / "background.db"))
    store._BACKEND = None
    from aiforge_core.runtime import chat_store
    chat_store.reset_backend_for_tests()
    bg_work._SCHEMA_DONE.clear()
    scheduler._STOP.clear()
    scheduler._PROCS.clear()
    scheduler._SESSION_AGENT.clear()
    scheduler._RUNNING.clear()
    yield tmp_path
    scheduler._STOP.clear()
    scheduler._PROCS.clear()
    scheduler._SESSION_AGENT.clear()
    scheduler._RUNNING.clear()
    store._BACKEND = None
    chat_store.reset_backend_for_tests()


def _session(isolated):
    from aiforge_core.runtime import chat_store
    return chat_store.create_session("gap")["id"]


def _texts(sid):
    from aiforge_core.runtime import chat_store
    return [m.get("content") or "" for m in chat_store.get_messages(sid)]


# ── 1. a finished job reports into the chat ────────────────────────────


def test_a_finished_agent_run_posts_a_short_line(isolated, monkeypatch):
    sid = _session(isolated)
    seen = {}

    def fake(messages, *, cwd, role, session_id):
        seen["session_id"] = session_id
        yield {"type": "message", "text": "Sent the digest.\n" + ("log line\n" * 40)}

    monkeypatch.setattr(
        "aiforge_core.runtime.chat_agent.run_chat_agent", fake)
    job = store.create(
        name="digest", cron="0 9 * * *", ticket_title="digest",
        ticket_body="read the queue", next_run_at="2020-01-01T00:00:00",
        kind="agent", session_id=sid)
    assert scheduler._fire_agent(job) is True
    assert _wait(lambda: any("digest" in t for t in _texts(sid)))
    posted = [t for t in _texts(sid) if "digest" in t][0]
    assert "Sent the digest." in posted
    assert "log line" not in posted
    assert len(posted) < 400
    assert seen["session_id"] == sid


def test_a_job_with_no_session_does_not_invent_one(isolated, monkeypatch):
    seen = {}

    def fake(messages, *, cwd, role, session_id):
        seen["session_id"] = session_id
        yield {"type": "message", "text": "ok"}

    monkeypatch.setattr(
        "aiforge_core.runtime.chat_agent.run_chat_agent", fake)
    job = store.create(
        name="page", cron="0 9 * * *", ticket_title="t", ticket_body="do it",
        next_run_at="2020-01-01T00:00:00", kind="agent")
    scheduler._fire_agent(job)
    assert _wait(lambda: "session_id" in seen)
    assert seen["session_id"] is None


# ── 2. chat can schedule the agent, tickets keep the floor ─────────────


def test_agent_schedule_may_be_more_frequent_than_a_ticket(isolated, tmp_path):
    agent = _watch._t_schedule_task(
        {"action": "create", "name": "often", "instruction": "check the queue",
         "every_minutes": 5, "kind": "agent", "until": "2h"}, str(tmp_path))
    assert agent["ok"], agent
    assert agent["kind"] == "agent"
    assert "webhook" in agent["note"]
    ticket = _watch._t_schedule_task(
        {"action": "create", "name": "often-ticket",
         "instruction": "file it", "every_minutes": 5, "kind": "ticket"},
        str(tmp_path))
    assert ticket["ok"] is False
    assert "floor" in ticket["error"]


def test_chat_schedule_remembers_the_session(isolated, monkeypatch, tmp_path):
    from aiforge_core.runtime import chat_cancel
    sid = _session(isolated)
    monkeypatch.setattr(chat_cancel, "active", lambda: sid)
    res = _watch._t_schedule_task(
        {"action": "create", "name": "later", "instruction": "summarise",
         "cron": "0 9 * * *", "kind": "agent", "until": "2h"}, str(tmp_path))
    assert res["ok"], res
    assert store.get(res["job_id"])["session_id"] == sid


# ── 3. background command does not hold the turn ───────────────────────


def test_background_command_survives_the_tool_return_and_stop_kills_it(
        isolated, monkeypatch):
    from aiforge_core.runtime import chat_cancel
    from aiforge_core.runtime.chat_agent._shell import _t_run_command
    sid = _session(isolated)
    monkeypatch.setattr(chat_cancel, "active", lambda: sid)
    a = b = None
    try:
        a = _t_run_command(
            {"cmd": "sleep 30", "background": True}, str(isolated))
        b = _t_run_command(
            {"cmd": "sleep 30", "background": True}, str(isolated))
        assert a["ok"] and a["background"] and b["background"]
        assert a["pid"] != b["pid"]
        assert _alive(a["pid"]) and _alive(b["pid"])
        assert bg_work.stop_session(sid) >= 1
        assert _wait(lambda: not _alive(a["pid"]) and not _alive(b["pid"]))
    finally:
        bg_work.stop_session(sid)


def test_a_trailing_ampersand_is_not_killed_on_return(isolated, monkeypatch):
    from aiforge_core.runtime import chat_cancel
    from aiforge_core.runtime.chat_agent._shell import _t_run_command
    sid = _session(isolated)
    monkeypatch.setattr(chat_cancel, "active", lambda: sid)
    res = _t_run_command({"cmd": "sleep 30 &"}, str(isolated))
    assert res.get("background") is True
    try:
        # The shell must still be the command, not a shell that already exited
        # and left an untracked child.
        assert _alive(res["pid"])
        assert _wait(lambda: _group_alive(res["pgid"]))
        bg_work.stop_session(sid)
        assert _wait(lambda: not _alive(res["pid"]))
    finally:
        bg_work.stop_session(sid)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def _group_alive(pgid) -> bool:
    """True when some process other than us still has this process group.

    ``ps -g`` means a process group on Linux and a group-id on macOS, so
    match the pgid column instead."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["ps", "-ax", "-o", "pid=,pgid="], text=True,
            stderr=subprocess.DEVNULL)
    except Exception:
        return False
    want = str(int(pgid))
    me = str(os.getpid())
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == want and parts[0] != me:
            return True
    return False


# ── 4. steer or stop a run that is already going ───────────────────────


def test_extra_detail_steers_a_scheduled_agent_and_drop_stops_it(isolated):
    from aiforge_core.api.routes._chat._message import _fold_into_scheduled_agent
    from aiforge_core.runtime import chat_interject
    sid = _session(isolated)
    ev = threading.Event()
    scheduler._STOP[77] = ev
    scheduler._SESSION_AGENT[sid] = 77
    chat_interject.set_steerable(sid, True)
    try:
        folded = _fold_into_scheduled_agent(sid, "also add a log line")
        assert folded is not None
        assert ev.is_set() is False
        chat_interject.clear(sid)
        chat_interject.set_steerable(sid, True)
        _fold_into_scheduled_agent(sid, "don't forget the date")
        assert ev.is_set() is False
        assert chat_interject.pending(sid)
        assert any("keeps going" in t for t in _texts(sid))
        chat_interject.clear(sid)
        _fold_into_scheduled_agent(sid, "drop that")
        assert ev.is_set() is True
    finally:
        scheduler._STOP.pop(77, None)
        scheduler._SESSION_AGENT.pop(sid, None)
        chat_interject.clear(sid)
        chat_interject.set_steerable(sid, False)


def test_cancel_kills_a_running_script_worker(isolated):
    from aiforge_core.jobs import scripts
    path = scripts.write_script("nap", "sleep 30\n")
    job = store.create(
        name="nap", cron="0 3 * * *", ticket_title="nap", ticket_body="sleep",
        next_run_at="2020-01-01T00:00:00", kind="script", script_path=path)
    assert scheduler._fire_script(job) is True
    assert _wait(lambda: scheduler._PROCS.get(job["id"]))
    pid = next(iter(scheduler._PROCS[job["id"]]))
    assert _alive(pid)
    res = _watch._t_schedule_task(
        {"action": "cancel", "job_id": job["id"]}, str(isolated))
    assert res["ok"] is True
    assert _wait(lambda: not _alive(pid))
    assert store.get(job["id"]) is None


# ── 5. webhook starts an existing job, with the API's own auth ────────


def test_webhook_fires_the_job_and_is_not_an_open_sync_path(
        isolated, monkeypatch):
    from aiforge_core.api._bind_security import _is_sync_path
    from aiforge_core.api.routes.jobs import jobs_webhook, router
    job = store.create(
        name="hooked", cron="0 9 * * *", ticket_title="t", ticket_body="b",
        next_run_at="2099-01-01T00:00:00", kind="agent")
    fired = {}

    def _fire(row, now=None):
        fired["id"] = row["id"]
        return True

    monkeypatch.setattr(scheduler, "fire", _fire)
    out = jobs_webhook(job["id"])
    assert out["ok"] is True
    assert fired["id"] == job["id"]
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/api/jobs/{job_id}/webhook" in paths
    assert _is_sync_path("/api/jobs/1/webhook") is False


# ── 6. several watches in one chat ─────────────────────────────────────


def test_two_watches_return_at_once_and_run_together(isolated, monkeypatch):
    from aiforge_core.runtime import chat_cancel
    sid = _session(isolated)
    monkeypatch.setattr(chat_cancel, "active", lambda: sid)
    started = []
    release = threading.Event()

    def _block(args, cwd):
        started.append(args.get("cmd"))
        release.wait(5)
        return {"ok": True, "matched": True, "checks": 1,
                "reason": "command exited 0"}

    monkeypatch.setattr(_watch, "_watch_until_blocking", _block)
    t0 = time.monotonic()
    a = _watch._t_watch_until({"cmd": "one", "timeout_s": 30}, str(isolated))
    b = _watch._t_watch_until({"cmd": "two", "timeout_s": 30}, str(isolated))
    assert time.monotonic() - t0 < 2
    assert a["background"] and b["background"] and a["handle"] != b["handle"]
    assert _wait(lambda: set(started) == {"one", "two"})
    release.set()
    assert _wait(lambda: any("Watch finished" in t for t in _texts(sid)))


def test_gitlab_watch_in_a_chat_does_not_hold_the_turn(isolated, monkeypatch):
    from aiforge_core.runtime import chat_cancel
    from aiforge_core.runtime.chat_agent._tools._gitlab import (
        _t_gitlab_pipeline_watch)
    from aiforge_core.runtime.tools import gitlab
    sid = _session(isolated)
    monkeypatch.setattr(chat_cancel, "active", lambda: sid)
    release = threading.Event()
    called = {}

    def _slow(args, cwd=None):
        called["args"] = args
        release.wait(5)
        return {"ok": True, "status": "success", "passed": True, "finished": True}

    monkeypatch.setattr(gitlab, "gitlab_pipeline_watch", _slow)
    t0 = time.monotonic()
    res = _t_gitlab_pipeline_watch({"project": "grp/p", "pipeline_id": 1},
                                   str(isolated))
    assert time.monotonic() - t0 < 2
    assert res["background"] is True
    release.set()
    assert _wait(lambda: any("Pipeline watch finished" in t for t in _texts(sid)))


def test_an_inline_gitlab_watch_still_runs_in_the_turn(isolated, monkeypatch):
    from aiforge_core.runtime import chat_cancel
    from aiforge_core.runtime.chat_agent._tools._gitlab import (
        _t_gitlab_pipeline_watch)
    from aiforge_core.runtime.tools import gitlab
    monkeypatch.setattr(chat_cancel, "active", lambda: 5)

    def _now(args, cwd=None):
        return {"ok": True, "inline": True, "status": "success"}

    monkeypatch.setattr(gitlab, "gitlab_pipeline_watch", _now)
    res = _t_gitlab_pipeline_watch(
        {"project": "grp/p", "inline": True}, str(isolated))
    assert res.get("inline") is True
    assert res.get("background") is not True


# ── 7. a claimed run is retried once after restart ─────────────────────


def test_an_inflight_job_is_retried_once(isolated, monkeypatch):
    job = store.create(
        name="half", cron="0 9 * * *", ticket_title="t", ticket_body="b",
        next_run_at="2020-01-01T09:00:00", kind="agent", session_id=None)
    assert store.claim(job["id"], expected_next_run_at=job["next_run_at"],
                       last_run_at="2020-01-01T09:00:00",
                       next_run_at="2020-01-02T09:00:00")
    dispatched = []
    monkeypatch.setattr(scheduler, "_dispatch",
                        lambda row: dispatched.append(row["id"]) or True)
    assert scheduler.resume_inflight() == 1
    assert dispatched == [job["id"]]
    assert store.get(job["id"])["run_attempt"] == 2
    assert scheduler.resume_inflight() == 0
    assert dispatched == [job["id"]]
    row = store.get(job["id"])
    assert row["run_status"] is None
    assert "already retried" in (row["last_error"] or "")


def test_a_background_watch_restarts_once_then_is_marked_stopped(
        isolated, monkeypatch):
    sid = _session(isolated)
    wid = bg_work._insert(sid, "watch", str(isolated), {"args": {"cmd": "true"}})
    spawned = []
    monkeypatch.setattr(bg_work, "_spawn", lambda i: spawned.append(i))
    assert bg_work.resume_after_restart() == 1
    assert spawned == [wid]
    assert bg_work._get(wid)["attempt"] == 2
    spawned.clear()
    # Still running, already retried: the next startup must not run it again.
    assert bg_work.resume_after_restart() == 0
    assert spawned == []
    assert bg_work._get(wid)["status"] == "stopped"
    assert any("already retried" in t for t in _texts(sid))


def test_the_chat_binding_is_visible_before_the_worker_runs(
        isolated, monkeypatch):
    from aiforge_core.runtime import chat_cancel
    sid = _session(isolated)
    job = store.create(
        name="bound", cron="0 9 * * *", ticket_title="t",
        ticket_body="do the thing", next_run_at="2020-01-01T09:00:00",
        kind="agent", session_id=sid)
    seen = {}
    real_start = threading.Thread.start

    def _start(self):
        if "bound" not in seen:
            seen["bound"] = scheduler.running_agent_for_session(sid)
            seen["token"] = chat_cancel.get(sid)
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", _start)
    monkeypatch.setattr(scheduler, "_run_agent_job", lambda *a, **k: None)
    try:
        assert scheduler._fire_agent(job) is True
        assert seen["bound"] == job["id"]
        assert seen["token"] is not None
        assert _wait(lambda: scheduler.running_agent_for_session(sid) is None)
    finally:
        scheduler._SESSION_AGENT.pop(sid, None)
        scheduler._RUNNING.discard(job["id"])
        chat_cancel.finish(sid)


def test_a_finished_job_does_not_drop_a_later_cancel_token():
    from aiforge_core.runtime import chat_cancel
    sid = 9042
    first = chat_cancel.start(sid)
    later = chat_cancel.start(sid)
    try:
        assert chat_cancel.finish_if_owner(sid, first) is False
        assert chat_cancel.get(sid) is later
    finally:
        chat_cancel.finish(sid)


def test_kill_all_stops_a_background_row_with_no_session(isolated, monkeypatch):
    killed = []
    monkeypatch.setattr(bg_work, "_kill", lambda pgid: killed.append(pgid))
    wid = bg_work._insert(
        None, "command", str(isolated), {"cmd": "true"}, pid=None, pgid=424242)
    assert bg_work.stop_all() >= 1
    assert 424242 in killed
    assert bg_work._get(wid)["status"] == "stopped"


def test_a_drop_message_ends_a_background_watch(isolated):
    from aiforge_core.api.routes._chat._message import _cut_background_watches
    sid = _session(isolated)
    wid = bg_work._insert(
        sid, "watch", str(isolated), {"args": {"cmd": "true"}})
    _cut_background_watches(sid, "also add a log line")
    assert bg_work._get(wid)["status"] == "running"
    _cut_background_watches(sid, "drop that")
    assert bg_work._get(wid)["status"] == "stopped"


def test_a_live_script_is_not_started_again(isolated, monkeypatch):
    import subprocess
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    job = store.create(
        name="still", cron="0 9 * * *", ticket_title="t", ticket_body="b",
        next_run_at="2020-01-01T09:00:00", kind="script", script_path="/tmp/x")
    assert store.claim(
        job["id"], expected_next_run_at=job["next_run_at"],
        last_run_at="2020-01-01T09:00:00", next_run_at="2020-01-02T09:00:00")
    store.update(job["id"], run_pid=proc.pid)
    dispatched = []
    monkeypatch.setattr(
        scheduler, "_dispatch", lambda row: dispatched.append(row["id"]) or True)
    try:
        assert scheduler.resume_inflight() == 0
        assert dispatched == []
        assert proc.poll() is None
        assert job["id"] in scheduler._STOP
    finally:
        scheduler.request_stop(job["id"])
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


def test_request_stop_clears_inflight_before_the_event_exists(isolated):
    job = store.create(
        name="early", cron="0 9 * * *", ticket_title="t", ticket_body="b",
        next_run_at="2020-01-01T09:00:00", kind="script")
    assert store.claim(
        job["id"], expected_next_run_at=job["next_run_at"],
        last_run_at="2020-01-01T09:00:00", next_run_at="2020-01-02T09:00:00")
    assert store.get(job["id"])["run_status"] == "running"
    assert job["id"] not in scheduler._STOP
    assert scheduler.request_stop(job["id"]) is True
    row = store.get(job["id"])
    assert row["run_status"] is None
    assert row["run_token"] is None
    # No leftover Event — the next fire must not bind an already-set stop.
    assert job["id"] not in scheduler._STOP


def test_a_worker_exits_when_the_claim_was_already_dropped(isolated, monkeypatch):
    job = store.create(
        name="gone", cron="0 9 * * *", ticket_title="t", ticket_body="b",
        next_run_at="2020-01-01T09:00:00", kind="script", script_path="/nope")
    assert store.claim(
        job["id"], expected_next_run_at=job["next_run_at"],
        last_run_at="2020-01-01T09:00:00", next_run_at="2020-01-02T09:00:00")
    job = store.get(job["id"])
    scheduler.request_stop(job["id"])
    started = []
    monkeypatch.setattr(
        "aiforge_core.jobs.scripts.run_script",
        lambda *a, **k: started.append(1) or {"ok": True})
    assert scheduler._fire_script(job) is True
    assert _wait(lambda: job["id"] not in scheduler._STOP)
    assert started == []


def test_dont_forget_does_not_end_a_background_watch(isolated):
    from aiforge_core.api.routes._chat._message import _cut_background_watches
    sid = _session(isolated)
    wid = bg_work._insert(
        sid, "watch", str(isolated), {"args": {"cmd": "true"}})
    _cut_background_watches(sid, "don't forget the date")
    assert bg_work._get(wid)["status"] == "running"
    _cut_background_watches(sid, "use grep instead")
    assert bg_work._get(wid)["status"] == "running"
    _cut_background_watches(sid, "stop that")
    assert bg_work._get(wid)["status"] == "stopped"


def test_late_script_error_does_not_land_on_a_reclaimed_job(isolated, monkeypatch):
    """A later claim owns last_error. The old worker must not overwrite it."""
    started = threading.Event()
    go = threading.Event()

    def _slow(path, on_start=None):
        if on_start is not None:
            class _P:
                pid = 4242
            on_start(_P())
        started.set()
        go.wait(5)
        return {"ok": False, "error": "late boom", "stderr": "old worker"}

    monkeypatch.setattr("aiforge_core.jobs.scripts.run_script", _slow)
    job = store.create(
        name="reclaim-script", cron="0 9 * * *", ticket_title="t",
        ticket_body="b", next_run_at="2020-01-01T09:00:00", kind="script",
        script_path="/x.sh")
    assert store.claim(
        job["id"], expected_next_run_at=job["next_run_at"],
        last_run_at="2020-01-01T09:00:00", next_run_at="2020-01-02T09:00:00")
    job = store.get(job["id"])
    stale = job["run_token"]
    assert scheduler._fire_script(job) is True
    assert _wait(lambda: started.is_set())
    assert store.claim(
        job["id"], expected_next_run_at="2020-01-02T09:00:00",
        last_run_at="2020-01-02T09:00:00", next_run_at="2020-01-03T09:00:00")
    fresh = store.get(job["id"])["run_token"]
    assert fresh != stale
    go.set()
    assert _wait(lambda: job["id"] not in scheduler._STOP)
    row = store.get(job["id"])
    assert row["run_token"] == fresh
    assert row["last_error"] is None


def test_late_agent_error_does_not_land_on_a_reclaimed_job(isolated, monkeypatch):
    started = threading.Event()
    go = threading.Event()

    def fake(messages, *, cwd, role, session_id):
        started.set()
        go.wait(5)
        yield {"type": "error", "text": "stale crash"}

    monkeypatch.setattr(
        "aiforge_core.runtime.chat_agent.run_chat_agent", fake)
    job = store.create(
        name="reclaim-agent", cron="0 9 * * *", ticket_title="t",
        ticket_body="do it", next_run_at="2020-01-01T09:00:00", kind="agent")
    assert store.claim(
        job["id"], expected_next_run_at=job["next_run_at"],
        last_run_at="2020-01-01T09:00:00", next_run_at="2020-01-02T09:00:00")
    job = store.get(job["id"])
    stale = job["run_token"]
    assert scheduler._fire_agent(job) is True
    assert _wait(lambda: started.is_set())
    assert store.claim(
        job["id"], expected_next_run_at="2020-01-02T09:00:00",
        last_run_at="2020-01-02T09:00:00", next_run_at="2020-01-03T09:00:00")
    fresh = store.get(job["id"])["run_token"]
    assert fresh != stale
    go.set()
    assert _wait(lambda: not scheduler.is_running(job["id"]))
    row = store.get(job["id"])
    assert row["run_token"] == fresh
    assert row["last_error"] is None
    assert "stale crash" not in (row["last_error"] or "")


def test_only_one_resume_wins_the_inflight_row(isolated):
    job = store.create(
        name="once", cron="0 9 * * *", ticket_title="t", ticket_body="b",
        next_run_at="2020-01-01T09:00:00", kind="agent")
    assert store.claim(
        job["id"], expected_next_run_at=job["next_run_at"],
        last_run_at="2020-01-01T09:00:00", next_run_at="2020-01-02T09:00:00")
    assert store.bump_inflight(job["id"], 1) is True
    assert store.bump_inflight(job["id"], 1) is False
    assert store.get(job["id"])["run_attempt"] == 2


# ── 8. Notification hook, and stdout the model can see ────────────────


def test_the_system_prompt_still_formats(isolated):
    from aiforge_core.runtime.chat_agent._prompt_text import _SYSTEM
    text = _SYSTEM.format(cwd="/tmp/work")
    assert "kind \"agent\"" in text
    assert "/api/jobs/{id}/webhook" in text
    assert "background" in text


def test_notification_hook_stdout_is_capped_and_visible(isolated, monkeypatch):
    from aiforge_core.config import _filecache
    monkeypatch.delenv("AIFORGE_HOOK_STDOUT_MAX", raising=False)
    cfg = isolated / "cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    blob = "Z" * 8000
    (cfg / "hooks.json").write_text(
        '{"Notification": [{"command": "printf \'' + blob + '\'"}],'
        ' "PreToolUse": [{"matcher": "*", "command": "echo blocked; exit 1",'
        ' "block_on_nonzero": true}]}')
    _filecache.clear()
    note = hooks.fire("Notification", {"reason": "finished"}, str(cfg))
    text = hooks.context_note(note)
    assert text.startswith("Z")
    assert "truncated" in text
    assert 1400 < len(text) <= 1600
    convo = []
    hooks.note_into(convo, "Notification", note)
    assert convo and convo[0]["role"] == "user"
    assert "not the user" in convo[0]["content"]
    assert "truncated" in convo[0]["content"]
    blocked = hooks.fire("PreToolUse", {"tool": "run_command"}, str(cfg))
    assert blocked["blocked"] is True
    _filecache.clear()
