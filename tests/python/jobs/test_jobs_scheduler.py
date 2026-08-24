"""Scheduler fire/tick with an injected clock. No sleeping, no threads —
run_loop is a trivial wrapper over tick() and is not tested here."""
from __future__ import annotations

from datetime import datetime

import pytest

from aiforge_core.jobs import scheduler, store

NOW = datetime(2026, 7, 2, 12, 0, 0)


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))


@pytest.fixture
def created(monkeypatch):
    calls: list[dict] = []

    class _T:
        id = 42
        identifier = "T-42"

    def fake_create(**kw):
        calls.append(kw)
        return _T()

    monkeypatch.setattr("aiforge_core.tickets.store.create", fake_create)
    return calls


def _mk(**over):
    base = dict(name="digest", cron="0 8 * * *",
                ticket_title="Pull GitLab comments",
                ticket_body="Fetch and summarize.", project="demo",
                next_run_at="2026-07-02T08:00:00")   # overdue vs NOW
    base.update(over)
    return store.create(**base)


def test_fire_creates_ticket_with_metadata(created):
    j = _mk()
    scheduler.fire(j, now=NOW)
    assert len(created) == 1
    kw = created[0]
    assert kw["title"] == "Pull GitLab comments"
    assert kw["project"] == "demo"
    assert kw["metadata"] == {"source": "scheduled_job", "job_id": j["id"]}
    got = store.get(j["id"])
    assert got["last_run_at"] == "2026-07-02T12:00:00"
    assert got["next_run_at"] == "2026-07-03T08:00:00"   # from NOW, not slot
    assert got["last_error"] is None


def test_fire_failure_records_error_and_still_advances(monkeypatch):
    def boom(**kw):
        raise RuntimeError("store down")
    monkeypatch.setattr("aiforge_core.tickets.store.create", boom)
    j = _mk()
    scheduler.fire(j, now=NOW)
    got = store.get(j["id"])
    assert "store down" in got["last_error"]
    assert got["next_run_at"] == "2026-07-03T08:00:00"   # advanced — no hot loop


def test_fire_impossible_date_cron_disables_job(created):
    # "0 0 31 2 *" passes croniter.is_valid at save time but get_next
    # raises (Feb 31 never occurs) — fire must disable the job, not crash,
    # and must NOT create a ticket.
    j = _mk(cron="0 0 31 2 *")
    assert scheduler.fire(j, now=NOW) is False
    assert len(created) == 0
    got = store.get(j["id"])
    assert got["enabled"] is False
    assert "unschedulable" in got["last_error"]


def test_fire_advance_failure_creates_no_ticket(monkeypatch, created):
    # If the schedule-advance write fails, NO ticket is created (at-most-
    # once): a transient jobs.db failure skips the run rather than
    # duplicating tickets on the next tick.
    j = _mk()
    calls = {"n": 0}

    # fire() now advances via the atomic store.claim (CAS) instead of mark_fired.
    def flaky_claim(*a, **k):
        calls["n"] += 1
        raise RuntimeError("jobs.db locked")

    monkeypatch.setattr(store, "claim", flaky_claim)
    assert scheduler.fire(j, now=NOW) is False
    assert len(created) == 0          # advance failed BEFORE create — no duplicate
    assert calls["n"] == 1            # claim was attempted
    # monkeypatch auto-restores store.claim at teardown.
    assert store.get(j["id"])["next_run_at"] == "2026-07-02T08:00:00"  # unchanged


def test_tick_fires_due_skips_future_and_disabled(created):
    _mk(name="due")
    _mk(name="future", next_run_at="2099-01-01T00:00:00")
    paused = _mk(name="paused")
    store.update(paused["id"], enabled=False)
    fired = scheduler.tick(now=NOW)
    assert fired == 1
    assert len(created) == 1


def test_tick_backlog_collapses_to_one_run(created):
    # 3 missed days — next_run_at far in the past. ONE fire, recomputed
    # from now: catch-up-once semantics.
    j = _mk(next_run_at="2026-06-29T08:00:00")
    assert scheduler.tick(now=NOW) == 1
    assert store.get(j["id"])["next_run_at"] == "2026-07-03T08:00:00"
    assert scheduler.tick(now=NOW) == 0   # nothing left due


def test_tick_one_bad_job_never_blocks_others(monkeypatch):
    calls = []

    class _T:
        id = 1
        identifier = "T-1"

    def flaky(**kw):
        calls.append(kw)
        if kw["title"] == "bad":
            raise RuntimeError("boom")
        return _T()

    monkeypatch.setattr("aiforge_core.tickets.store.create", flaky)
    _mk(name="bad", ticket_title="bad")
    good = _mk(name="good", ticket_title="good")
    fired = scheduler.tick(now=NOW)
    assert fired == 1                       # only the good one counts
    assert len(calls) == 2                  # both were attempted
    assert store.get(good["id"])["last_error"] is None
