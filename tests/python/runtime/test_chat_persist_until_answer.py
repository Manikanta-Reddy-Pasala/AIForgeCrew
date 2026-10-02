"""A step the model keeps failing is retried (waiting, smaller prompt) until it
answers — the task finishes. Only Stop, a typed message, an explicit bound or a
configuration error ends it."""
import pytest

from aiforge_core.runtime import run_interrupt
from aiforge_core.runtime.chat_agent._turn import _completion as C


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_LLM_RETRIES", "0")
    monkeypatch.setenv("AIFORGE_CHAT_PERSIST_S", "0")
    monkeypatch.setattr(run_interrupt, "pause", lambda *a, **k: None)
    monkeypatch.setattr(C, "_PERSIST_GAPS", (0.0,))


def _drive(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


def _failing_then(n_fail, answer="FINAL: done"):
    calls = {"n": 0}

    def fn(role, convo):
        calls["n"] += 1
        if calls["n"] <= n_fail:
            raise RuntimeError("HTTP 500 internal error")
        return answer
    return fn, calls


def _run(fn, convo=None, session_id=None):
    convo = convo if convo is not None else [{"role": "system", "content": "s"}]
    return _drive(C._retry_completion(
        fn, "doer", convo, session_id, RuntimeError("HTTP 500 internal error"),
        None, None, None, wait_s=None))


def test_a_step_that_fails_many_times_still_completes():
    fn, calls = _failing_then(6)
    events, out = _run(fn)
    assert out == "FINAL: done"
    assert calls["n"] == 7
    assert not any(e.get("type") == "stopped" for e in events)
    assert any("trying again" in e.get("text", "") for e in events)


def test_the_old_stop_is_one_setting_away(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PERSIST_S", "-1")
    fn, calls = _failing_then(99)
    events, out = _run(fn)
    assert out is C._RETRY_STOP
    assert any(e.get("type") == "stopped" for e in events)
    assert calls["n"] == 0                       # no retries at all


def test_a_bound_ends_it(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PERSIST_S", "0.0001")
    monkeypatch.setattr(C, "_PERSIST_GAPS", (1.0,))
    fn, _ = _failing_then(99)
    events, out = _run(fn)
    assert out is C._RETRY_STOP


def test_stop_ends_it(monkeypatch):
    monkeypatch.setattr(run_interrupt, "pause", lambda *a, **k: "stop")
    fn, calls = _failing_then(99)
    _events, out = _run(fn)
    assert out is C._CANCELLED
    assert calls["n"] == 0


def test_a_model_that_is_not_served_is_reported_not_retried(monkeypatch):
    from aiforge_core.llm import client
    monkeypatch.setattr(client, "model_missing", lambda exc: True)
    fn, calls = _failing_then(99)
    events, out = _run(fn)
    assert out is C._RETRY_STOP
    assert calls["n"] == 0


def test_the_prompt_is_condensed_between_attempts(monkeypatch):
    seen = []

    def shrink(convo, role, complete_fn, session_id):
        seen.append(len(convo))
        return False
    monkeypatch.setattr(C, "_shrink_for_retry", shrink)
    fn, _ = _failing_then(2)
    _run(fn)
    assert len(seen) == 3
