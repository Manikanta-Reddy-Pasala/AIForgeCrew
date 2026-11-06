"""A watch, background command or scheduled job working in a chat shows in
the progress panel, and the chat counts as busy though no turn holds it."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import chat_background as CB


@pytest.fixture(autouse=True)
def _dbs(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_BG_DB_PATH", str(tmp_path / "bg.db"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))
    from aiforge_core.jobs import store
    store.reset_backend_for_tests()
    yield
    store.reset_backend_for_tests()


def _watch(sid, cmd="curl -s localhost:8080/health", until="exit_zero", status="running"):
    from aiforge_core.runtime import bg_work
    wid = bg_work._insert(sid, "watch", "/tmp", {"args": {"cmd": cmd, "until": until}})
    if status != "running":
        con = bg_work._connect()
        con.execute("UPDATE bg SET status=? WHERE id=?", (status, wid))
        con.commit()
        con.close()
    return wid


def test_a_running_watch_is_listed_and_makes_the_chat_busy():
    _watch(7)
    items = CB.items(7)
    assert [i["kind"] for i in items] == ["watch"] and items[0]["state"] == "running"
    assert "curl -s localhost:8080/health" in items[0]["title"]
    assert CB.active(7) and 7 in CB.active_sessions()
    assert not CB.items(8) and not CB.active(8)


def test_a_finished_watch_is_not():
    _watch(7, status="done")
    assert CB.items(7) == [] and not CB.active(7)


def test_a_schedule_of_the_chat_is_listed_with_its_next_run(monkeypatch):
    from aiforge_core.jobs import scheduler, store
    job = store.create(name="check prod health", cron="*/10 * * * *", ticket_title="t",
                       ticket_body="b", kind="agent", session_id=7,
                       next_run_at="2030-01-01T00:00:00Z")
    items = CB.items(7)
    assert items and items[0]["kind"] == "schedule" and items[0]["state"] == "scheduled"
    assert items[0]["next_run_at"] and not CB.active(7)       # waiting is not busy
    monkeypatch.setattr(scheduler, "is_running", lambda jid: jid == job["id"])
    assert CB.items(7)[0]["state"] == "running" and CB.active(7) and 7 in CB.active_sessions()


def test_a_broken_store_lists_what_it_can():
    import aiforge_core.jobs.store as store
    orig = store.list_jobs
    store.list_jobs = lambda: (_ for _ in ()).throw(RuntimeError("db gone"))
    try:
        _watch(7)
        assert [i["kind"] for i in CB.items(7)] == ["watch"]
    finally:
        store.list_jobs = orig


def test_a_script_job_claimed_in_the_store_is_running_and_a_paused_one_still_shows(monkeypatch):
    from aiforge_core.jobs import store
    job = store.create(name="nightly sync", cron="0 2 * * *", ticket_title="t", ticket_body="b",
                       kind="script", session_id=7, next_run_at="2030-01-01T00:00:00")
    store.update(job["id"], run_status="running", enabled=False)
    items = CB.items(7)
    assert items[0]["state"] == "running" and 7 in CB.active_sessions()
    assert items[0]["next_run_at"].startswith("2030-01-01T00:00:00") and items[0]["next_run_at"][19:]  # offset
    store.update(job["id"], run_status=None)
    assert CB.items(7) == []                                     # paused and idle: gone


def test_background_stop_stops_watches_only():
    _watch(7)
    out = CB.stop(7)
    assert out["watches"] == 1 and CB.items(7) == []


def test_a_schedule_shows_its_recent_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_JOB_RUNS_DB_PATH", str(tmp_path / "runs.db"))
    from aiforge_core.jobs import run_log, store
    job = store.create(name="health", cron="*/10 * * * *", ticket_title="t", ticket_body="b",
                       kind="agent", session_id=7, next_run_at="2030-01-01T00:00:00")
    run_log.record(job["id"], started_at="2026-11-06T10:00:00", ok=True)
    run_log.record(job["id"], started_at="2026-11-06T10:10:00", ok=False, note="exit 1")
    runs = CB.items(7)[0]["runs"]
    assert [r["ok"] for r in runs] == [False, True] and runs[0]["note"] == "exit 1"
    assert runs[0]["started_at"].startswith("2026-11-06T10:10:00")
    for _ in range(run_log.KEEP + 5):
        run_log.record(job["id"], ok=True)
    assert len(run_log.recent(job["id"], 100)) == run_log.KEEP


def test_side_tasks_running_queued_and_recent_ones_are_listed(monkeypatch):
    import time
    from aiforge_core.runtime import chat_runs, chat_store
    now = time.time()
    kids = [{"id": 11, "title": "lint", "task": {"state": "running", "started": now - 60}},
            {"id": 12, "title": "docs", "task": {"state": "queued"}},
            {"id": 13, "title": "tests", "task": {"state": "done", "started": now - 300, "ended": now - 100}},
            {"id": 14, "title": "old", "task": {"state": "done", "ended": now - 10 * 3600}},
            {"id": 15, "title": "gone", "task": {"state": "running", "started": now - 600}}]
    monkeypatch.setattr(chat_store, "child_sessions", lambda pid: kids)
    monkeypatch.setattr(chat_runs, "is_running", lambda sid: sid == 11)
    rows = [r for r in CB.items(7) if r["kind"] == "side_task"]
    assert [(r["title"], r["state"]) for r in rows] == [("lint", "running"), ("docs", "queued"),
                                                        ("tests", "done"), ("gone", "stopped")]
    items = CB.items(7)
    assert CB.active(7) and not CB.stoppable(items)         # busy, but its stop is its own


def test_one_run_is_one_history_row_and_a_stop_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_JOB_RUNS_DB_PATH", str(tmp_path / "runs.db"))
    from aiforge_core.jobs import run_log, scheduler
    job = {"id": 5, "last_run_at": "2026-11-06T10:00:00", "run_token": "tok-a"}
    scheduler._log_run(job, ok=True)
    scheduler._log_run(job, ok=False, note="raised after the post")
    scheduler._log_run(dict(job), ok=None, note="stopped")        # a stop racing the worker
    assert [r["ok"] for r in run_log.recent(5)] == [1]
    scheduler._log_run({**job, "run_token": "tok-b"}, ok=None, note="stopped")
    assert [r["ok"] for r in run_log.recent(5)] == [None, 1]
