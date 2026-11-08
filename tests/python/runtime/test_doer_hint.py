"""The first change of a big request is held once with the run_doers note;
small requests, doers, plan/strict runs and approvals-on chats are not asked."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._tools import _doers as D
from aiforge_core.runtime.chat_agent._turn import _doer_hint as H


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    toks = [(v, v.set(d)) for v, d in ((D.LEAD_DEADLINE, None), (D.IN_DOER, False))]
    monkeypatch.setattr(D, "_approvals_on", lambda sid: False)
    monkeypatch.delenv("AIFORGE_DOER_DEADLINE_S", raising=False)
    monkeypatch.setenv("AIFORGE_CHAT_DOER_HINT", "1")
    yield
    for v, t in reversed(toks):
        v.reset(t)


def _st(goal="x" * 2000, **kw):
    base = dict(goal=goal, board={}, session_id=1, readonly_mode=False, plan_mode=False,
                builder="", strict_finish=False, doer_hinted=False, convo=[])
    base.update(kw)
    return SimpleNamespace(**base)


def _run(st, name="file_write", args=None):
    g = H.gate(st, {}, name, args or {"path": "a.py", "content": "x"})
    events = []
    try:
        while True:
            events.append(next(g))
    except StopIteration as stop:
        return stop.value, events


def test_big_request_first_change_is_held_once():
    st = _st()
    sig, events = _run(st)
    assert sig == "continue" and events and "run_doers" in st.convo[-1]["content"]
    assert _run(st) == (None, [])                       # asked once only


def test_not_asked_for_small_or_read_or_doer_or_strict():
    assert _run(_st(goal="fix the typo in a.py")) == (None, [])
    assert _run(_st(), name="read_lines", args={"path": "a.py"}) == (None, [])
    assert _run(_st(strict_finish=True)) == (None, [])
    assert _run(_st(plan_mode=True)) == (None, [])
    assert _run(_st(session_id=None)) == (None, [])
    tok = D.IN_DOER.set(True)
    try:
        assert _run(_st()) == (None, [])
    finally:
        D.IN_DOER.reset(tok)


def test_not_asked_with_approvals_on_or_little_time(monkeypatch):
    import time
    monkeypatch.setattr(D, "_approvals_on", lambda sid: True)
    assert _run(_st()) == (None, [])
    monkeypatch.setattr(D, "_approvals_on", lambda sid: False)
    tok = D.LEAD_DEADLINE.set(time.monotonic() + 400)
    try:
        assert _run(_st()) == (None, [])
    finally:
        D.LEAD_DEADLINE.reset(tok)


def test_calling_run_doers_counts_as_answered_and_switch_turns_it_off(monkeypatch):
    st = _st()
    assert _run(st, name="run_doers", args={"parts": []}) == (None, [])
    assert st.doer_hinted and _run(st) == (None, [])
    monkeypatch.setenv("AIFORGE_CHAT_DOER_HINT", "0")
    assert _run(_st()) == (None, [])


def test_work_runs_and_non_file_writes_are_not_asked():
    from aiforge_core.runtime import chat_goals
    with chat_goals.off_for_run():                       # a scheduled job
        assert _run(_st()) == (None, [])

    def agent():
        yield _run(_st())
    assert list(H.quiet(agent())) == [(None, [])]       # the Plan pipeline's Doer
    assert not H._QUIET.get()
    assert _run(_st(), name="jira_create_issue",
                args={"summary": "x", "project": "P"}) == (None, [])


def test_the_hint_runs_before_the_plan_first_gate():
    import inspect
    from aiforge_core.runtime.chat_agent import _loop
    src = inspect.getsource(_loop._gated_action)
    assert src.index("_doer_hint.gate") < src.index("_plan_first.gate")
    assert "next to your call" in H.HINT


def test_off_unless_turned_on(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_DOER_HINT", raising=False)
    assert not H.enabled() and _run(_st()) == (None, [])
