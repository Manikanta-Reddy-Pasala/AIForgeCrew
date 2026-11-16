"""An unanswered question: the model decides and continues — never for a
destructive one."""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _ask_wait as A


def _st(**kw):
    return SimpleNamespace(**{"convo": [], "plan_mode": False, **kw})


def test_destructive_questions_still_wait(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_ASK_WAIT_S", raising=False)
    st = _st()
    for q in ("Should I delete the old migrations?", "Drop the users table and recreate it?",
              "Reset the branch to origin/main?", "OK to overwrite config.yaml?"):
        assert not A.may_decide(st, q), q
    assert A.may_decide(st, "Should the report use the invoice date or the due date?")
    assert not A.may_decide(_st(plan_mode=True), "Which file format?")


def test_switch_and_per_turn_limit(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_ASK_WAIT_S", "0")
    assert not A.may_decide(_st(), "Which format?")
    monkeypatch.delenv("AIFORGE_CHAT_ASK_WAIT_S")
    assert not A.may_decide(_st(auto_decided=5), "Which format?")


def test_silence_tells_the_model_to_decide(monkeypatch):
    monkeypatch.setattr(A, "wait_for_answer", lambda sid, secs: "timeout")
    st = _st()
    gen = A.ask_and_wait(st, 7, "CSV or JSON?")
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        assert stop.value == "continue"
    assert events[0]["text"] == "CSV or JSON?"
    assert "make the call yourself" in st.convo[-1]["content"] and st.auto_decided == 1


def test_an_answer_is_left_for_the_steering_drain(monkeypatch):
    monkeypatch.setattr(A, "wait_for_answer", lambda sid, secs: "answered")
    st = _st()
    gen = A.ask_and_wait(st, 7, "CSV or JSON?")
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        assert stop.value == "continue"
    assert "answer to your question follows" in st.convo[-1]["content"]


def test_wait_polls_the_queue_and_stops_on_cancel(monkeypatch):
    from aiforge_core.runtime import chat_cancel, chat_interject
    clock = {"t": 0.0}
    monkeypatch.setattr(chat_interject, "pending", lambda sid: clock["t"] >= 3)
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: False)
    got = A.wait_for_answer(1, 90, _sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                            _now=lambda: clock["t"])
    assert got == "answered"
    monkeypatch.setattr(chat_interject, "pending", lambda sid: False)
    assert A.wait_for_answer(1, 5, _sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                             _now=lambda: clock["t"]) == "timeout"
    assert A.wait_for_answer(None, 90) == "timeout"
