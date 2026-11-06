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
