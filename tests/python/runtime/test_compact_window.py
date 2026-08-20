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


def test_api_and_window_agree_on_the_hour(monkeypatch):
    """One parser, not two — the scheduled pass and the chat folds must not
    disagree about when the window opens."""
    from aiforge_core.api import api
    for raw in ("18", "off", "0", "24", "99", "nonsense"):
        monkeypatch.setenv("AIFORGE_COMPACT_AT_HOUR", raw)
        assert api._compact_at_hour() == compact_window.at_hour()


# ── scheduler: no catch-up before the hour ──────────────────────────────

def _task(strict, at_hour=18, seen=None, last=None, ok=True):
    t = periodic._Task(name=f"t-{strict}-{at_hour}", fn=lambda: None,
                       at_hour=at_hour, strict_hour=strict)
    t._rec[0] = {"at": last, "ok": ok, "fails": 0, "seen": seen}
    return t


def test_missed_slot_catches_up_at_any_hour_by_default():
    now = datetime(2026, 8, 20, 9, 0)                   # morning, day missed
    t = _task(False, seen=now - timedelta(days=5))
    # Due now (only the boot grace stands between it and running).
    assert t._daily_next(0.0, now) <= periodic._GRACE_S + periodic._SPREAD_S


def test_strict_hour_defers_a_missed_slot_to_todays_hour():
    now = datetime(2026, 8, 20, 9, 0)
    t = _task(True, seen=now - timedelta(days=5))
    wait = t._daily_next(0.0, now)
    assert wait == 9 * 3600                             # waits until 18:00 today
    # …and at 18:00 it is due.
    t2 = _task(True, seen=now - timedelta(days=5))
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


def test_register_passes_the_flag_through():
    periodic._TASKS.clear()
    periodic.register("strict-task", lambda: None, at_hour=18, strict_hour=True)
    periodic.register("loose-task", lambda: None, at_hour=18)
    by_name = {t.name: t for t in periodic._TASKS}
    assert by_name["strict-task"].strict_hour is True
    assert by_name["loose-task"].strict_hour is False
    periodic._TASKS.clear()


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
