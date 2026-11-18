"""Keep going: stuck / out of budget starts over with a handoff instead of stopping."""
import time
from types import SimpleNamespace

from aiforge_core.runtime import chat_keep_going as kg


def _st(on=True):
    st = SimpleNamespace(convo=[{"role": "system", "content": "sys"},
                                {"role": "user", "content": "do the task"}],
                         cwd="/nonexistent", stuck_escalations=7, wrapping_up=True)
    kg.set_for(41, on)
    kg.start(st, 41)
    return st


def test_flag_is_per_session():
    kg.set_for(1, True)
    kg.set_for(2, False)
    assert kg.on(1) and not kg.on(2) and not kg.on(None)
    kg.set_for(1, False)
    assert not kg.on(1)


def test_fresh_start_resets_the_stuck_state_and_tells_the_model(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _escalate
    monkeypatch.setattr(_escalate, "restart_with_handoff", lambda st: True)
    st = _st()
    ev = list(kg.fresh_start(st, "got stuck"))
    assert st.persist_used == 1 and st.stuck_escalations == 0 and st.wrapping_up is False
    assert "keep going" in st.convo[-1]["content"] and "DIFFERENT approach" in st.convo[-1]["content"]
    assert any("fresh start 1/" in e["text"] for e in ev)


def test_bounded_by_retries_and_time(monkeypatch):
    st = _st()
    monkeypatch.setenv("AIFORGE_CHAT_PERSIST_RETRIES", "2")
    assert kg.may_restart(st)
    st.persist_used = 2
    assert not kg.may_restart(st)
    st.persist_used = 0
    monkeypatch.setenv("AIFORGE_CHAT_PERSIST_MAX_S", "60")
    st.persist_t0 = time.monotonic() - 61
    assert not kg.may_restart(st)
    assert not kg.may_restart(_st(on=False))


def test_the_stuck_ladder_continues_instead_of_giving_up(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn._stuck import ladder

    def wrap(st, why):
        yield {"type": "thought", "text": "trip"}
        return "wrap_up"
    monkeypatch.setattr(ladder, "escalate", wrap)
    monkeypatch.setattr(kg, "fresh_start", lambda st, why: iter([{"type": "thought", "text": "fresh"}]))
    st = _st()
    gen = ladder.change_approach(st, "same step again")
    out = []
    try:
        while True:
            out.append(next(gen))
    except StopIteration as e:
        result = e.value
    assert result == "continue" and not any(e.get("type") == "done" for e in out)
    st.persist = False                                   # setting off: the old give-up
    monkeypatch.setattr("aiforge_core.runtime.chat_agent._turn._best_state.restore_if_worse",
                        lambda st: iter(()))
    gen = ladder.change_approach(st, "same step again")
    out = []
    try:
        while True:
            out.append(next(gen))
    except StopIteration as e:
        result = e.value
    assert result == "return" and any(e.get("type") == "done" for e in out)
