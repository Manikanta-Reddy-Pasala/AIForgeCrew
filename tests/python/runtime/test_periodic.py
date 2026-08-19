"""One internal recurring-task engine — register + fire on interval/daily,
debounced, launched through background.spawn.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta

import pytest


def test_register_and_fire_interval(monkeypatch):
    from aiforge_core.runtime import periodic as p
    # isolate the module-global task list
    monkeypatch.setattr(p, "_TASKS", [])
    monkeypatch.setattr(p, "_started", False)
    fired = []
    p.register("unit", lambda: fired.append(1), every_s=0.5, debounce_s=0.0)
    p.start()
    for _ in range(40):
        if fired:
            break
        time.sleep(0.1)
    assert fired


def test_register_is_idempotent(monkeypatch):
    from aiforge_core.runtime import periodic as p
    monkeypatch.setattr(p, "_TASKS", [])
    p.register("dup", lambda: None, every_s=10)
    p.register("dup", lambda: None, every_s=10)
    assert sum(1 for t in p._TASKS if t.name == "dup") == 1


def test_at_hour_due_calc(monkeypatch, tmp_path):
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))   # own state file
    now = datetime.now()
    t = p._Task("x", lambda: None, at_hour=now.hour)
    # same hour already started → next fire is ~tomorrow (positive, < 25h)
    s = t._next_after(0.0, now)
    assert 0 <= s <= 25 * 3600


def test_disabled(monkeypatch):
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_PERIODIC_DISABLE", "1")
    monkeypatch.setattr(p, "_TASKS", [])
    monkeypatch.setattr(p, "_started", False)
    p.register("x", lambda: None, every_s=1)
    p.start()
    assert p._started is False        # start() no-op when disabled


def _task(p, name="evening", **kw):
    """A daily task with the boot grace already elapsed (loop timing is not
    what these assert)."""
    t = p._Task(name, kw.pop("fn", lambda: None), at_hour=kw.pop("at_hour", 18), **kw)
    t._hold[0] = 0.0
    return t


def _mono(p):
    """A monotonic reading past the boot grace + the widest task spread."""
    return p._BOOT + p._GRACE_S + p._SPREAD_S + 1


def _ran(p, t, when, *, ok=True, fails=0, seen=None):
    """Give ``t`` a run record as if it had last been attempted at ``when``."""
    t._rec[0] = {"at": when, "ok": ok, "fails": fails, "seen": seen or when}
    t._persist()


def test_at_hour_is_at_or_after_and_fires_once_a_day(monkeypatch, tmp_path):
    """A daily task must not be lost when the loop wakes a hair late.

    time.sleep only overshoots, so "next occurrence of HH:00" was skipped for a
    whole day whenever the wake landed past it.
    """
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    now = datetime.now().replace(hour=18, minute=0, second=0, microsecond=42)
    t = _task(p)
    assert t._next_after(_mono(p), now) == 0.0        # due, though :00 has passed
    # later the same day, still not run → still due (a laptop asleep at 18:00)
    assert t._next_after(_mono(p), now.replace(hour=23, minute=59)) == 0.0
    # once it has run, next due is tomorrow's hour
    _ran(p, t, now)
    s = t._next_after(_mono(p), now.replace(hour=19))
    assert 22 * 3600 < s <= 24 * 3600


def test_at_hour_before_the_hour_waits(monkeypatch, tmp_path):
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    now = datetime.now().replace(hour=9, minute=30, second=0, microsecond=0)
    t = _task(p)
    assert t._next_after(_mono(p), now) == pytest.approx(8.5 * 3600, abs=1)


def test_missed_day_catches_up_at_the_next_wake(monkeypatch, tmp_path):
    """A 9-5 laptop never reaches 18:00 — without catch-up it never compacts.

    The slot is "once a day, from 18:00", not "18:00 sharp": a full day with no
    run makes the task due at the next wake, whatever the hour.
    """
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    now = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    t = _task(p)
    _ran(p, t, now - timedelta(hours=39))            # missed yesterday's slot
    assert t._next_after(_mono(p), now) == 0.0
    # ran YESTERDAY evening (15h ago) → today's slot has not passed, so wait
    _ran(p, t, (now - timedelta(days=1)).replace(hour=18))
    assert t._next_after(_mono(p), now) == pytest.approx(9 * 3600, abs=1)


def test_catch_up_survives_a_restart_when_it_has_never_run(monkeypatch, tmp_path):
    """The baseline is PERSISTED: an in-process one resets every restart, so a
    machine that is never up at 18:00 would never become due at all."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    day1 = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    _task(p)._next_after(_mono(p), day1)             # first sighting, anchors "seen"
    assert "evening" in p._load_state()
    fresh = _task(p)                                 # restarted, still never ran
    assert fresh._next_after(_mono(p), day1 + timedelta(hours=8)) > 0.0
    assert fresh._next_after(_mono(p), day1 + timedelta(days=1, hours=1)) == 0.0


def test_a_failed_run_retries_then_waits_for_the_next_day(monkeypatch, tmp_path):
    """Unbounded retry would turn "once a day" back into hourly forever."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    now = datetime.now().replace(hour=18, minute=30, second=0, microsecond=0)
    t = _task(p)
    _ran(p, t, now - timedelta(minutes=10), ok=False, fails=1)
    assert t._next_after(_mono(p), now) == pytest.approx(50 * 60, abs=1)  # backoff
    _ran(p, t, now - timedelta(hours=2), ok=False, fails=1)
    assert t._next_after(_mono(p), now) == 0.0                            # retry due
    _ran(p, t, now - timedelta(hours=2), ok=False, fails=p._MAX_FAILS)
    assert t._next_after(_mono(p), now) > 3600                            # gave up


def test_a_failed_run_is_recorded_so_restarts_do_not_buy_more_attempts(
        monkeypatch, tmp_path):
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    now = datetime.now().replace(hour=18, minute=30, second=0, microsecond=0)
    _ran(p, _task(p), now - timedelta(minutes=5), ok=False, fails=p._MAX_FAILS)
    assert _task(p)._next_after(_mono(p), now) > 3600     # fresh process, same verdict


def test_a_future_run_record_is_clamped_not_dropped(monkeypatch, tmp_path):
    """A dead RTC, a restored snapshot or a clock moved back writes a stamp
    ahead of the clock. Dropping it also drops the 12h floor, so the heavy pass
    runs a second time the same day; clamping keeps the floor and the task
    resumes normally tomorrow — not 30 days later."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    now = datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)
    t = _task(p)
    _ran(p, t, now + timedelta(days=30))
    s = t._next_after(_mono(p), now)
    assert 22 * 3600 < s <= 24 * 3600                  # tomorrow's slot, not locked
    assert p._load_state()["evening"]["at"].startswith(now.date().isoformat())


def test_clock_moved_back_an_hour_does_not_double_fire(monkeypatch, tmp_path):
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    ran_at = datetime.now().replace(hour=18, minute=5, second=0, microsecond=0)
    t = _task(p)
    _ran(p, t, ran_at)
    for back in (1, 2, 6):                             # NTP / timezone / snapshot
        assert t._next_after(_mono(p), ran_at - timedelta(hours=back)) > 0.0


def test_at_hour_zero_does_not_double_fire_around_midnight(monkeypatch, tmp_path):
    """at_hour=0: "the hour has passed" is true all day, so only the run record
    stands between a 23:30 fire and another one 30 minutes later."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    late = datetime.now().replace(hour=23, minute=30, second=0, microsecond=0)
    t = _task(p, at_hour=0)
    assert t._next_after(_mono(p), late) == 0.0
    _ran(p, t, late)
    assert t._next_after(_mono(p), late + timedelta(minutes=30)) > 0.0


def test_at_hour_run_survives_restart(monkeypatch, tmp_path):
    """A restart after the hour must not re-run an LLM-heavy daily task."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    now = datetime.now()
    _ran(p, _task(p, at_hour=0), now)
    fresh = _task(p, at_hour=0)                      # fresh process, same disk
    assert fresh._next_after(_mono(p), now) > 0.0
    assert p._load_state()["evening"]["at"].startswith(now.date().isoformat())


def test_state_from_an_older_bare_stamp_still_reads(monkeypatch, tmp_path):
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    today = datetime.now().date().isoformat()
    (tmp_path / "periodic_state.json").write_text(f'{{"evening": "{today}"}}')
    rec = _task(p)._record(datetime.now())
    assert rec["at"].date().isoformat() == today and rec["ok"] is True


def test_corrupt_state_is_moved_aside(monkeypatch, tmp_path):
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "periodic_state.json").write_text("{not json")
    assert p._load_state() == {}
    assert (tmp_path / "periodic_state.corrupt").exists()


def test_fire_records_the_run_and_a_failure_is_persisted(monkeypatch, tmp_path):
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(p, "_TASKS", [])
    fired = []
    t = _task(p, at_hour=0, debounce_s=0.0, fn=lambda: fired.append(1))
    p._fire(t)
    for _ in range(40):
        if fired:
            break
        time.sleep(0.1)
    assert fired
    assert p._load_state()["evening"]["ok"] is True

    def _boom():
        raise RuntimeError("nope")

    bad = _task(p, name="bad", at_hour=0, debounce_s=0.0, fn=_boom)
    p._fire(bad)
    for _ in range(40):
        if bad._hold[0]:
            break
        time.sleep(0.1)
    assert bad._hold[0] > time.monotonic()           # held off, not spinning
    rec = p._load_state()["bad"]
    assert rec["ok"] is False and rec["fails"] == 1  # the ATTEMPT is on disk


def test_fire_stamps_the_loop_clock_not_its_own(monkeypatch, tmp_path):
    """A wake at 23:59:59.99x must not book the run against tomorrow."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(p, "_TASKS", [])
    when = (datetime.now() - timedelta(days=1)).replace(hour=23, minute=59,
                                                         second=59)
    t = _task(p, at_hour=0, debounce_s=0.0)
    p._fire(t, when)
    at = p._load_state()["evening"]["at"]
    assert at.startswith(when.date().isoformat())      # the LOOP's clock, not now()
    assert not at.startswith(datetime.now().date().isoformat())


def test_due_daily_task_waits_out_the_boot_grace(monkeypatch, tmp_path):
    """Three heavy daily jobs must not all land in the API's first second."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    now = datetime.now().replace(hour=23, minute=0, second=0, microsecond=0)
    boot = p._BOOT + 0.5                       # half a second of uptime
    delays = {n: p._Task(n, lambda: None, at_hour=18)._next_after(boot, now)
              for n in ("daily-compact", "graph-maintain", "memory-dedup")}
    assert all(d >= p._GRACE_S - 1 for d in delays.values())
    assert len(set(delays.values())) == 3      # spread apart, not one second


def test_retry_count_carries_within_a_day_and_resets_the_next(monkeypatch, tmp_path):
    """One token (`fails if same_day else 0`) is the whole retry cap: reset it
    every fire and a permanently failing pass is hourly again; never reset it
    and one bad day disables the task forever."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(p, "_TASKS", [])
    when = datetime.now().replace(hour=18, minute=0, second=0, microsecond=0)

    def _boom():
        raise RuntimeError("nope")

    def _disk_fails(p, want):
        for _ in range(50):
            if p._load_state().get("evening", {}).get("fails") == want:
                return want
            time.sleep(0.1)
        return p._load_state().get("evening", {}).get("fails")

    t = _task(p, at_hour=18, debounce_s=0.0, fn=_boom)
    for attempt in (1, 2):
        t._last[0] = 0.0
        p._fire(t, when + timedelta(hours=attempt - 1))
        assert _disk_fails(p, attempt) == attempt          # carried, same day
    t._last[0] = 0.0
    p._fire(t, when + timedelta(days=1))                   # a new day
    assert _disk_fails(p, 1) == 1                          # reset, retries again


def test_a_run_in_flight_is_not_re_entered(monkeypatch, tmp_path):
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(p, "_TASKS", [])
    started, release = [], threading.Event()
    t = _task(p, at_hour=0, debounce_s=0.0,
              fn=lambda: (started.append(1), release.wait(5)))
    p._fire(t)
    for _ in range(40):
        if started:
            break
        time.sleep(0.1)
    t._last[0] = 0.0                       # debounce out of the way
    p._fire(t, datetime.now() + timedelta(days=1))
    assert len(started) == 1               # the second slot is skipped, not stacked
    release.set()


def test_a_poisoned_state_file_does_not_kill_the_loop(monkeypatch, tmp_path):
    """~/.aiforge/periodic_state.json is operator-visible; one wrong-typed value
    must not stop reindex, graph maintenance and compaction for good."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "periodic_state.json").write_text(json.dumps({
        "evening": {"at": "2026-08-19T18:00:00+05:30", "ok": True, "fails": "x"}}))
    t = _task(p)
    assert t._next_after(_mono(p), datetime.now()) >= 0.0      # no raise
    assert t._record(datetime.now())["fails"] == 0


def test_catch_up_fires_at_the_slot_not_an_hour_later(monkeypatch, tmp_path):
    """Measured against YESTERDAY's slot, not ">24h ago".

    An elapsed-time rule fires one hourly wake later each day, so the run walks
    forward and eventually falls out of a laptop's awake window entirely.
    """
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    now = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    t = _task(p)
    _ran(p, t, now - timedelta(hours=24))            # yesterday's catch-up run
    assert t._next_after(_mono(p), now) == 0.0       # due NOW, not at 10:00


def test_a_failed_run_does_not_retry_across_midnight(monkeypatch, tmp_path):
    """A retry that crossed midnight consumed the new day's slot and walked an
    at_hour=23 task around the clock."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    midnight = datetime.now().replace(hour=0, minute=30, second=0, microsecond=0)
    t = _task(p, at_hour=23)
    _ran(p, t, midnight - timedelta(hours=1, minutes=30), ok=False, fails=1)
    assert t._next_after(_mono(p), midnight) == pytest.approx(22.5 * 3600, abs=1)


def test_datetime_bounds_in_the_state_file_do_not_disable_a_task(monkeypatch,
                                                                 tmp_path):
    """The UTC→local conversion of a stamp near the datetime bounds raises —
    and the JSON is VALID, so nothing ever rewrites or renames it: the task
    would be dead for good."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for stamp in ("9999-12-31T23:59:59+00:00", "0001-01-01T00:00:00+14:00",
                  "not-a-timestamp"):
        (tmp_path / "periodic_state.json").write_text(json.dumps(
            {"evening": {"at": stamp, "ok": True, "fails": 0}}))
        t = _task(p, at_hour=0)
        parsed = p._parse_ts(stamp)                  # never raises
        assert parsed is None or isinstance(parsed, datetime)
        due = t._next_after(_mono(p), datetime.now())
        assert 0.0 <= due <= 25 * 3600               # scheduled, not dead forever


def test_infinite_fails_value_does_not_disable_a_task(monkeypatch, tmp_path):
    """json.loads maps 1e400 to inf, and int(inf) raises OverflowError."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "periodic_state.json").write_text(
        '{"evening": {"at": "", "ok": true, "fails": 1e400}}')
    assert _task(p)._record(datetime.now())["fails"] == 0


def test_a_task_that_cannot_launch_is_not_left_in_flight(monkeypatch, tmp_path):
    """background.spawn never raises — it returns None when the thread could
    not start, which used to leave _busy set for the life of the process."""
    from aiforge_core.runtime import background as bg
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(p, "_TASKS", [])
    monkeypatch.setattr(bg, "spawn", lambda *a, **k: None)
    t = _task(p, at_hour=0, debounce_s=0.0)
    p._fire(t)
    assert t._busy[0] is False
    assert p._load_state()["evening"]["ok"] is False    # not counted as a run


def test_one_broken_task_does_not_stop_the_others(monkeypatch, tmp_path):
    """_due() keeps a task whose state can't be read from killing the loop."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    bad = _task(p, name="bad")
    monkeypatch.setattr(bad, "_next_after",
                        lambda *a: (_ for _ in ()).throw(TypeError("boom")))
    assert p._due(bad, _mono(p), datetime.now()) == 3600.0


def test_concurrent_state_writes_keep_every_record(monkeypatch, tmp_path):
    """The failure record is written from a worker thread while the loop
    records other tasks — an unlocked read-modify-write loses it."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    names = [f"t{i}" for i in range(12)]

    def _writer(name):
        for i in range(40):
            p._save_entry(name, {"at": "", "ok": False, "fails": i + 1, "seen": ""})

    threads = [threading.Thread(target=_writer, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    st = p._load_state()
    assert all(st.get(n, {}).get("fails") == 40 for n in names)


def test_boot_grace_is_short_enough_for_a_short_lived_process(monkeypatch,
                                                              tmp_path):
    """A process that dies before grace+spread never runs its daily tasks at
    all — so the ceiling is asserted in ABSOLUTE seconds, not against the
    constants themselves."""
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    now = datetime.now().replace(hour=23, minute=0, second=0, microsecond=0)
    worst = max(p._Task(n, lambda: None, at_hour=18)._next_after(p._BOOT + 0.5, now)
                for n in ("daily-compact", "chat-compact", "recompact-all",
                          "graph-maintain", "memory-dedup", "reindex"))
    assert worst < 120.0
