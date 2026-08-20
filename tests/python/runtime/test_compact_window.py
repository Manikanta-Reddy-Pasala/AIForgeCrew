"""Compaction stays OUT of the working day.

Two ways it used to leak in: the scheduler's missed-slot catch-up (a laptop
asleep at 18:00 compacted at 09:00 the next morning) and the opportunistic fold
fired whenever the user opens a new chat.
"""
from datetime import datetime, timedelta

from aiforge_core.runtime import compact_window, periodic


# ── window helper ───────────────────────────────────────────────────────

def test_window_opens_at_the_configured_hour(monkeypatch):
    monkeypatch.setenv("AIFORGE_COMPACT_AT_HOUR", "18")
    assert compact_window.at_hour() == 18
    assert not compact_window.open_now(datetime(2026, 8, 20, 9, 59))
    assert not compact_window.open_now(datetime(2026, 8, 20, 17, 59))
    assert compact_window.open_now(datetime(2026, 8, 20, 18, 0))
    assert compact_window.open_now(datetime(2026, 8, 20, 23, 30))


def test_window_is_always_open_when_the_daily_pass_is_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_COMPACT_AT_HOUR", "off")
    assert compact_window.at_hour() is None
    assert compact_window.open_now(datetime(2026, 8, 20, 9, 0))


# ── scheduler: no catch-up before the hour ──────────────────────────────

def _task(strict, at_hour=18, seen=None, last=None, ok=True, skip_days=3):
    t = periodic._Task(name=f"t-{strict}-{at_hour}", fn=lambda: None,
                       at_hour=at_hour, strict_hour=strict,
                       strict_max_skip_days=skip_days)
    t._rec[0] = {"at": last, "ok": ok, "fails": 0, "seen": seen}
    return t


def test_missed_slot_catches_up_at_any_hour_by_default():
    now = datetime(2026, 8, 20, 9, 0)                   # morning, day missed
    t = _task(False, seen=now - timedelta(days=5))
    # Due now (only the boot grace stands between it and running).
    assert t._daily_next(0.0, now) <= periodic._GRACE_S + periodic._SPREAD_S


def test_strict_hour_defers_a_missed_slot_to_todays_hour():
    now = datetime(2026, 8, 20, 9, 0)
    # One day missed (inside the starvation floor) → waits for tonight.
    t = _task(True, seen=now - timedelta(days=1, hours=2))
    wait = t._daily_next(0.0, now)
    assert wait == 9 * 3600                             # waits until 18:00 today
    # …and at 18:00 it is due.
    t2 = _task(True, seen=now - timedelta(days=1, hours=2))
    assert t2._daily_next(0.0, datetime(2026, 8, 20, 18, 0)) <= (
        periodic._GRACE_S + periodic._SPREAD_S)


def test_strict_hour_does_not_retry_a_failed_pass_before_the_hour():
    """A failed attempt recorded in the morning (older build, clock change)
    must not buy an hourly retry through the working day."""
    now = datetime(2026, 8, 20, 10, 30)
    t = _task(True, last=datetime(2026, 8, 20, 9, 0), ok=False,
              seen=datetime(2026, 8, 1, 18, 0))
    assert t._daily_next(0.0, now) == 7.5 * 3600        # waits for 18:00
    # A failure AT/AFTER the hour still retries within the hour.
    t2 = _task(True, last=datetime(2026, 8, 20, 18, 0), ok=False,
               seen=datetime(2026, 8, 1, 18, 0))
    assert t2._daily_next(0.0, datetime(2026, 8, 20, 19, 30)) <= (
        periodic._GRACE_S + periodic._SPREAD_S)


def test_register_passes_the_flag_through(monkeypatch):
    monkeypatch.setattr(periodic, "_TASKS", [])
    periodic.register("strict-task", lambda: None, at_hour=18, strict_hour=True,
                      strict_max_skip_days=5)
    periodic.register("loose-task", lambda: None, at_hour=18)
    by_name = {t.name: t for t in periodic._TASKS}
    assert by_name["strict-task"].strict_hour is True
    assert by_name["strict-task"].strict_max_skip_days == 5
    assert by_name["loose-task"].strict_hour is False


# ── chat: the on-switch fold obeys the window ───────────────────────────

def test_fold_on_chat_switch_is_deferred_before_the_hour(monkeypatch):
    from aiforge_core.runtime import chat_session_fold
    monkeypatch.setenv("AIFORGE_COMPACT_AT_HOUR", "18")
    monkeypatch.setattr(compact_window, "open_now", lambda *a, **k: False)
    started = []
    monkeypatch.setattr(chat_session_fold.threading, "Thread",
                        lambda **kw: started.append(kw) or _NoThread())
    chat_session_fold.fold_async(7)
    assert started == []


def test_fold_on_chat_switch_runs_inside_the_window(monkeypatch):
    from aiforge_core.runtime import chat_session_fold
    monkeypatch.setenv("AIFORGE_COMPACT_AT_HOUR", "18")
    monkeypatch.setattr(compact_window, "open_now", lambda *a, **k: True)
    started = []

    class _T:
        def __init__(self, **kw):
            started.append(kw)

        def start(self):
            pass
    monkeypatch.setattr(chat_session_fold.threading, "Thread", _T)
    chat_session_fold.fold_async(7)
    assert len(started) == 1


class _NoThread:
    def start(self):                      # pragma: no cover — must never run
        raise AssertionError("fold started outside the compaction window")


def test_strict_hour_has_a_starvation_floor():
    """A laptop never awake at 18:00 must still compact eventually — "quiet"
    is not "never". After the floor, the catch-up fires whatever the hour."""
    now = datetime(2026, 8, 20, 9, 0)
    # 2 days missed, floor 3 → still waits for tonight.
    t = _task(True, seen=now - timedelta(days=2))
    assert t._daily_next(0.0, now) == 9 * 3600
    # 4 days missed → takes the catch-up now.
    t2 = _task(True, seen=now - timedelta(days=4))
    assert t2._daily_next(0.0, now) <= periodic._GRACE_S + periodic._SPREAD_S
    # floor disabled → starves by explicit choice.
    t3 = _task(True, seen=now - timedelta(days=40), skip_days=0)
    assert t3._daily_next(0.0, now) == 9 * 3600


def test_window_opens_when_no_daily_pass_will_run(monkeypatch):
    """Deferring "to the daily pass" is only safe if there IS one. With the
    periodic engine, jobs, or the registering startup handler disabled, the
    on-switch fold is the only compaction there is."""
    monkeypatch.setenv("AIFORGE_COMPACT_AT_HOUR", "18")
    morning = datetime(2026, 8, 20, 9, 0)
    assert not compact_window.open_now(morning)
    for var, val in (("AIFORGE_PERIODIC_DISABLE", "1"),
                     ("AIFORGE_JOBS_DISABLE", "1"),
                     ("AIFORGE_REINDEX_DAILY", "0")):
        monkeypatch.setenv(var, val)
        assert compact_window.open_now(morning), var
        monkeypatch.delenv(var)


def test_catch_up_knob_opens_both_halves(monkeypatch):
    """The escape hatch has to move the fold gate too — restoring the scheduler
    while leaving the fold dead 18 hours a day is not "the old behaviour"."""
    monkeypatch.setenv("AIFORGE_COMPACT_AT_HOUR", "18")
    assert not compact_window.open_now(datetime(2026, 8, 20, 9, 0))
    monkeypatch.setenv("AIFORGE_COMPACT_CATCH_UP", "1")
    assert compact_window.catch_up_enabled()
    assert compact_window.open_now(datetime(2026, 8, 20, 9, 0))


def test_startup_migration_folds_without_the_learner_outside_the_window(
        monkeypatch, tmp_path):
    """Every API boot used to run one learner call per brief — on a laptop that
    is every morning the lid opens, by a path the scheduler never sees. Outside
    the window the structural fold still runs; only the model is left out."""
    from aiforge_core.memory import md_store
    from aiforge_core.memory import migrations

    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_COMPACT_AT_HOUR", "18")
    seen: list = []
    monkeypatch.setattr(md_store, "compact",
                        lambda **kw: seen.append(kw.get("summarize")) or {"files_in": 0})
    monkeypatch.setattr(md_store, "sweep_stale_captures", lambda **_kw: {"swept": 0})

    monkeypatch.setattr(compact_window, "open_now", lambda *a, **k: False)
    migrations.run_startup_migrations()
    assert seen == [False, False]        # structural only — no learner calls

    seen.clear()
    monkeypatch.setattr(compact_window, "open_now", lambda *a, **k: True)
    migrations.run_startup_migrations()
    assert seen == [True, True]          # inside the window, fold as before

    seen.clear()
    monkeypatch.setattr(compact_window, "open_now", lambda *a, **k: False)
    monkeypatch.setenv("AIFORGE_STARTUP_COMPACT", "always")
    migrations.run_startup_migrations()
    assert seen == [True, True]          # explicit opt-out of the window

    seen.clear()
    monkeypatch.setenv("AIFORGE_STARTUP_COMPACT", "off")
    migrations.run_startup_migrations()
    assert seen == []                    # skipped entirely
