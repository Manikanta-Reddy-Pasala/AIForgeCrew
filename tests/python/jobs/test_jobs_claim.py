"""store.claim — atomic compare-and-swap on a job's due slot. Only the FIRST
racer wins; a second call on the already-advanced slot returns False. This is
what stops run-now + tick (or multi-replica) double-fires."""
from __future__ import annotations

import pytest

from aiforge_core.jobs import store


@pytest.fixture
def sqlite_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_JOBS_DB", str(tmp_path / "jobs.db"))
    # Force a fresh sqlite backend bound to the tmp db.
    monkeypatch.setattr(store, "_BACKEND", None)
    yield store
    monkeypatch.setattr(store, "_BACKEND", None)


def _mk(s):
    return s.create(name="j", cron="0 9 * * *", ticket_title="t",
                    ticket_body="b", next_run_at="2020-01-01T09:00:00")


def test_first_claim_wins_second_loses(sqlite_store):
    s = sqlite_store
    job = _mk(s)
    slot = job["next_run_at"]
    won1 = s.claim(job["id"], expected_next_run_at=slot,
                   last_run_at="2020-01-01T09:00:00",
                   next_run_at="2020-01-02T09:00:00")
    won2 = s.claim(job["id"], expected_next_run_at=slot,   # SAME old slot
                   last_run_at="2020-01-01T09:00:00",
                   next_run_at="2020-01-02T09:00:00")
    assert won1 is True
    assert won2 is False   # slot already advanced → no double-fire


def test_claim_advances_the_slot(sqlite_store):
    s = sqlite_store
    job = _mk(s)
    s.claim(job["id"], expected_next_run_at=job["next_run_at"],
            last_run_at="2020-01-01T09:00:00",
            next_run_at="2020-01-02T09:00:00")
    after = s.get(job["id"])
    assert after["next_run_at"].startswith("2020-01-02")


def test_claim_wrong_expected_slot_is_noop(sqlite_store):
    s = sqlite_store
    job = _mk(s)
    won = s.claim(job["id"], expected_next_run_at="1999-01-01T00:00:00",
                  last_run_at="2020-01-01T09:00:00",
                  next_run_at="2020-01-02T09:00:00")
    assert won is False
    assert s.get(job["id"])["next_run_at"].startswith("2020-01-01")
