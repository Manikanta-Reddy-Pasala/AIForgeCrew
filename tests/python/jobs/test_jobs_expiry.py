"""Every scheduled loop ends — by the user's words, or by itself in 2 hours.

The close is what these pin: the row goes, the LEARNING and the SCRIPT stay.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from aiforge_core.jobs import lifecycle, scheduler, store

NOW = datetime(2026, 8, 27, 10, 0, 0)


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))


@pytest.fixture()
def captured(monkeypatch):
    """Collect the learnings instead of writing them to the real memory."""
    seen: list[dict] = []
    monkeypatch.setattr(
        "aiforge_core.memory.md_store.capture",
        lambda kind, text, **kw: seen.append({"kind": kind, "text": text, **kw}),
    )
    return seen


def _mk(**over):
    base = dict(name="watch errors", cron="*/15 * * * *",
                ticket_title="watch errors", ticket_body="tail the error log",
                project=None, next_run_at="2026-08-27T10:15:00")
    base.update(over)
    return store.create(**base)


# ─── parse_until ────────────────────────────────────────────────────────

def test_no_until_gets_the_two_hour_default():
    """The whole point: say nothing about how long, and it still ends."""
    exp, err = lifecycle.parse_until(None, now=NOW)
    assert err is None
    assert exp == "2026-08-27T12:00:00"


def test_blank_until_is_the_same_as_none():
    assert lifecycle.parse_until("", now=NOW)[0] == "2026-08-27T12:00:00"


@pytest.mark.parametrize("raw,expected", [
    ("90m", "2026-08-27T11:30:00"),
    ("2h", "2026-08-27T12:00:00"),
    ("3d", "2026-08-30T10:00:00"),
    ("1w", "2026-09-03T10:00:00"),
    ("45", "2026-08-27T10:45:00"),          # bare number = minutes
    ("for 3 days", "2026-08-30T10:00:00"),  # the user's own phrasing
    ("until 2h", "2026-08-27T12:00:00"),
])
def test_durations(raw, expected):
    assert lifecycle.parse_until(raw, now=NOW)[0] == expected


def test_tomorrow_means_through_tomorrow_not_this_time_tomorrow():
    """Someone who says "until tomorrow" wants it alive when they look in the
    morning — end of that day, not 10:00."""
    assert lifecycle.parse_until("tomorrow", now=NOW)[0] == "2026-08-28T23:59:59"


def test_today_ends_tonight():
    assert lifecycle.parse_until("today", now=NOW)[0] == "2026-08-27T23:59:59"


def test_bare_iso_date_means_the_end_of_that_day():
    """Midnight would expire the job before its first fire on that date."""
    assert lifecycle.parse_until("2026-09-01", now=NOW)[0] == "2026-09-01T23:59:59"


def test_iso_datetime_is_taken_literally():
    assert lifecycle.parse_until("2026-08-28T06:30:00", now=NOW)[0] \
        == "2026-08-28T06:30:00"


def test_forever_is_the_only_way_to_never_expire():
    exp, err = lifecycle.parse_until("forever", now=NOW)
    assert (exp, err) == (None, None)


def test_past_is_refused_not_silently_expired():
    exp, err = lifecycle.parse_until("2020-01-01T00:00:00", now=NOW)
    assert exp is None
    assert "past" in err or "could not read" in err


def test_gibberish_explains_itself():
    exp, err = lifecycle.parse_until("when the vibes are right", now=NOW)
    assert exp is None
    assert "until" in err and "forever" in err


def test_absurd_horizon_is_capped_not_honoured():
    """"until 2099" is a typo, not a decade of cron fires."""
    exp, _ = lifecycle.parse_until("2099-01-01", now=NOW)
    assert exp == (NOW + timedelta(minutes=lifecycle.max_ttl_minutes())
                   ).isoformat(timespec="seconds")


def test_default_ttl_is_configurable(monkeypatch):
    monkeypatch.setenv("AIFORGE_JOB_DEFAULT_TTL_MINUTES", "30")
    assert lifecycle.parse_until(None, now=NOW)[0] == "2026-08-27T10:30:00"


# ─── store ──────────────────────────────────────────────────────────────

def test_expired_query_finds_the_past_and_ignores_the_future_and_null():
    past = _mk(name="past", expires_at="2026-08-27T09:00:00")
    _mk(name="future", expires_at="2026-08-27T23:00:00")
    _mk(name="forever")  # expires_at NULL
    got = store.expired_jobs("2026-08-27T10:00:00")
    assert [j["id"] for j in got] == [past["id"]]


def test_a_disabled_job_still_expires():
    """A paused loop is still a loop nobody closed."""
    j = _mk(expires_at="2026-08-27T09:00:00")
    store.update(j["id"], enabled=False)
    assert [x["id"] for x in store.expired_jobs("2026-08-27T10:00:00")] == [j["id"]]


# ─── close ──────────────────────────────────────────────────────────────

def test_close_keeps_the_learning_and_drops_the_row(captured):
    j = _mk(expires_at="2026-08-27T09:00:00")
    res = lifecycle.close_job(j, "reached its end time")
    assert res["ok"] is True
    assert res["learning_captured"] is True
    assert store.get(j["id"]) is None          # row gone
    assert captured[0]["kind"] == "learning"
    text = captured[0]["text"]
    assert "watch errors" in text and "reached its end time" in text
    assert "tail the error log" in text        # WHAT it watched survives


def test_close_names_the_script_it_kept(captured, tmp_path):
    script = tmp_path / "check.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    j = _mk(kind="script", script_path=str(script))
    res = lifecycle.close_job(j, "cancelled from chat")
    assert res["script_kept"] == str(script)
    assert script.exists(), "a script job's file belongs to the operator"
    assert str(script) in captured[0]["text"]


def test_close_records_that_it_was_failing(captured):
    j = _mk()
    store.update(j["id"], last_error="boom")
    lifecycle.close_job(store.get(j["id"]), "expired")
    assert "FAILING" in captured[0]["text"]


def test_a_broken_memory_does_not_strand_the_job(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("memory down")
    monkeypatch.setattr("aiforge_core.memory.md_store.capture", _boom)
    j = _mk()
    res = lifecycle.close_job(j, "expired")
    assert res["ok"] is True and res["learning_captured"] is False
    assert store.get(j["id"]) is None


def test_close_expired_sweeps_only_what_ended(captured):
    dead = _mk(name="dead", expires_at="2026-08-27T09:00:00")
    alive = _mk(name="alive", expires_at="2026-08-27T23:00:00")
    assert lifecycle.close_expired(NOW) == 1
    assert store.get(dead["id"]) is None
    assert store.get(alive["id"]) is not None


# ─── scheduler ──────────────────────────────────────────────────────────

def test_tick_closes_an_expired_job_instead_of_firing_it(captured, monkeypatch):
    """Due AND expired in the same tick: it closes, it does not get one last run."""
    fired: list[int] = []
    monkeypatch.setattr(scheduler, "fire",
                        lambda job, now=None: fired.append(job["id"]) or True)
    j = _mk(next_run_at="2026-08-27T09:00:00", expires_at="2026-08-27T09:30:00")
    scheduler.tick(NOW)
    assert fired == []
    assert store.get(j["id"]) is None


def test_fire_refuses_an_expired_job_reached_directly(captured):
    """run-now from the API reaches fire() without the sweep."""
    j = _mk(next_run_at="2026-08-27T09:00:00", expires_at="2026-08-27T09:30:00")
    assert scheduler.fire(store.get(j["id"]), now=NOW) is False
    assert store.get(j["id"]) is None


def test_a_live_job_still_fires(monkeypatch):
    created: list[str] = []

    class _T:
        identifier = "T-1"

    monkeypatch.setattr("aiforge_core.tickets.store.create",
                        lambda **kw: created.append(kw["title"]) or _T())
    j = _mk(next_run_at="2026-08-27T09:00:00", expires_at="2026-08-27T23:00:00")
    assert scheduler.fire(store.get(j["id"]), now=NOW) is True
    assert created == ["watch errors"]
    assert store.get(j["id"]) is not None


# ─── cleanup: keep the learning and the scripts, bin the rest ───────────

def test_close_keeps_scripts_out_of_the_workspace_and_deletes_the_rest(
        captured, monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    j = _mk(kind="agent")
    from aiforge_core.jobs import lifecycle as lc
    ws = lc.workspace_of(j)
    import os
    from pathlib import Path
    os.makedirs(os.path.join(ws, "checkout"), exist_ok=True)
    Path(ws, "check_health.sh").write_text("#!/bin/sh\ncurl -f x\n")
    Path(ws, "parse.py").write_text("print(1)\n")
    Path(ws, "run.log").write_text("noise\n" * 1000)
    Path(ws, "checkout", "deep.sh").write_text("nested\n")

    res = lc.close_job(j, "reached its end time")

    assert not os.path.exists(ws), "the scratch workspace is not left in /tmp"
    kept = [os.path.basename(p) for p in res["scripts_kept"]]
    assert any(k.endswith("check_health.sh") for k in kept)
    assert any(k.endswith("parse.py") for k in kept)
    assert not any("run.log" in k for k in kept), "logs are not worth keeping"
    assert all(os.path.exists(p) for p in res["scripts_kept"])
    assert os.access(res["scripts_kept"][0], os.X_OK) or \
        res["scripts_kept"][0].endswith(".py")
    # and the learning says where they went
    assert "check_health.sh" in captured[0]["text"]


def test_close_without_a_workspace_is_not_an_error(captured):
    j = _mk()
    res = lifecycle.close_job(j, "expired")
    assert res["ok"] is True and res["scripts_kept"] == []


def test_a_run_still_in_flight_keeps_its_workspace(captured, monkeypatch, tmp_path):
    """Closing the row is fine mid-run; deleting the directory the agent is
    working in is not."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    j = _mk(kind="agent")
    import os
    ws = lifecycle.workspace_of(j)
    os.makedirs(ws, exist_ok=True)
    from pathlib import Path
    Path(ws, "half_written.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(scheduler, "is_running", lambda jid: jid == j["id"])

    res = lifecycle.close_job(j, "reached its end time")

    assert res["ok"] is True            # the row still closes
    assert res["scripts_kept"] == []
    assert os.path.isdir(ws), "a working agent's directory is left alone"
    import shutil
    shutil.rmtree(ws, ignore_errors=True)
