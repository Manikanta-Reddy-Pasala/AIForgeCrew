"""A board item whose build, suite or deploy is still running is waiting, not
stuck: the timebox neither charges that time, nor sends the move-on note, nor
parks the item while the job's last check said it is running."""
from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._turn import _items as I
from aiforge_core.runtime.chat_agent._turn import _timebox as T
from aiforge_core.runtime.chat_agent._turn import _waiting as W


def _st(**kw):
    board = {"p1": {"title": "Build", "status": "running", "from_request": True},
             "p2": {"title": "Deploy", "status": "pending", "from_request": True}}
    base = dict(board=board, readonly_mode=False, builder=None, turn_deadline=None,
                turn_budget_s=None, extensions_used=0, ext_budget=2, item_clock=None,
                time_warned=False, deadline_final=None, item_log=[], item_start=0,
                convo=[])
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(I, "active", lambda st: True)
    monkeypatch.setenv("AIFORGE_CHAT_ITEM_TIMEBOX_S", "900")
    monkeypatch.setenv("AIFORGE_CHAT_ITEM_PARK_GRACE_S", "300")
    monkeypatch.setenv("AIFORGE_CHAT_JOB_WAIT_MAX_S", "7200")


def _run(st, start, until, step=100.0):
    notes, t = [], start
    while t <= until:
        n = T.check(st, now=t)
        if n:
            notes.append(t)
        t += step
    return notes


def test_a_running_job_is_not_charged_and_draws_no_note():
    st = _st()
    T.check(st, now=0.0)
    st.job_waits = {"job-1": 0.0}           # a 40-min build, checked on and running
    assert _run(st, 100.0, 2400.0) == []
    assert st.item_clock.get("p1", 0.0) == 0.0
    assert T.park_due(st, now=2400.0) is None


def test_the_clock_runs_again_once_the_job_ends():
    st = _st()
    T.check(st, now=0.0)
    st.job_waits = {"job-1": 0.0}
    _run(st, 100.0, 2400.0)
    st.job_waits = {}                       # the job finished
    notes = _run(st, 2500.0, 4600.0)
    assert notes, "an item stuck AFTER its job ends still gets the note"
    # 900 s limit, doubled before any request part closes
    assert notes[0] >= 2500.0 + 1800.0 - 100.0


def test_a_job_past_the_wait_cap_counts_as_stuck(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_JOB_WAIT_MAX_S", "600")
    st = _st()
    T.check(st, now=0.0)
    st.job_waits = {"job-1": 0.0}
    assert _run(st, 100.0, 2800.0), "a job that never ends is stuck again after the cap"


def test_no_park_while_the_job_runs_even_after_the_note():
    st = _st()
    _run(st, 0.0, 1000.0)                   # note sent at ~1000 s
    st.job_waits = {"job-1": 1000.0}        # then a long suite starts
    _run(st, 1100.0, 2500.0)
    assert T.park_due(st, now=2500.0) is None


def test_job_running_reads_only_live_waits():
    st = SimpleNamespace()
    assert W.job_running(st) is False
    st.job_waits = {"a": 0.0}
    assert W.job_running(st, now=100.0) is True
