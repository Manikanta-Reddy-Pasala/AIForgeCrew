"""One ReAct step must not cost sixty generations.

Three retry layers sit on top of each other — the transport re-attempts a
broken call (AIFORGE_LLM_RETRY_MAX, 3), the client re-posts an empty answer
(AIFORGE_LLM_EMPTY_RETRIES, 3 → 4 posts) and the chat loop sweeps again
(AIFORGE_CHAT_LLM_RETRIES, 5). They MULTIPLY, and every one of those
generations ships the whole prompt and produces an answer nobody reads. A
model that is failing for a structural reason does not answer better on the
twentieth try.
"""
from __future__ import annotations

import threading

import pytest

from aiforge_core.llm import call_meter
from aiforge_core.runtime.chat_agent import _loop


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    call_meter.reset_all()
    monkeypatch.setattr(_loop.time, "sleep", lambda *_a: None)
    yield
    call_meter.reset_all()


def _run(session_id=77, **env):
    """Drive one step whose completion always fails, counting attempts."""
    calls = {"n": 0}

    def _complete(*_a, **_k):
        calls["n"] += 1
        # every attempt is a real request as far as the meter is concerned
        call_meter.record("chat", session_id=session_id)
        raise RuntimeError("llm.exhausted role=chat")

    out = list(_loop.run_chat_agent(
        [{"role": "user", "content": "hi"}], session_id=session_id,
        cwd=".", complete_fn=_complete))
    return calls["n"], out


def test_the_sweep_stops_at_the_per_step_ceiling(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_LLM_RETRIES", "5")
    monkeypatch.setenv("AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP", "3")
    n, out = _run()
    assert n <= 3, f"{n} generations for one step"
    assert any(e.get("type") == "message" for e in out)   # still answers the user


def test_the_ceiling_can_be_lifted(monkeypatch):
    """0 = no ceiling, the old per-layer behaviour — not "no retries", which is
    what a naive `budget - spent` clamp would have produced."""
    monkeypatch.setenv("AIFORGE_CHAT_LLM_RETRIES", "2")
    monkeypatch.setenv("AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP", "0")
    n, _ = _run(session_id=78)
    assert n == 3          # the first call + its 2 sweeps


def test_a_bad_ceiling_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP", "not-a-number")
    assert _loop._max_gen_per_step() == 6


def test_the_ceiling_bounds_the_PRODUCT_not_the_sweep_count(monkeypatch):
    """One sweep is not one generation: below this loop the client re-posts an
    empty answer and the transport re-attempts a broken one, so a sweep can
    burn four. Sampling the spend once and extrapolating let a declared ceiling
    of 6 spend 12 — the exact multiplication the ceiling exists to stop."""
    monkeypatch.setenv("AIFORGE_CHAT_LLM_RETRIES", "5")
    monkeypatch.setenv("AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP", "6")
    sid = 91
    gens = {"n": 0}

    def _complete(*_a, **_k):
        for _ in range(4):            # what one call really costs
            gens["n"] += 1
            call_meter.record("chat", session_id=sid)
        raise RuntimeError("llm.exhausted role=chat")

    list(_loop.run_chat_agent([{"role": "user", "content": "hi"}],
                              session_id=sid, cwd=".", complete_fn=_complete))
    assert gens["n"] <= 8, f"{gens['n']} generations against a ceiling of 6"


def _run_unattended(gens_per_call=3, **_kw):
    """The same failing step, with NO session — jobs, text_doer, the analysis
    fan-out and parallel subtasks all call the loop this way."""
    calls = {"n": 0}

    def _complete(*_a, **_k):
        for _ in range(gens_per_call):
            calls["n"] += 1
            call_meter.record("doer")          # no session, like the real thing
        raise RuntimeError("llm.exhausted role=doer")

    list(_loop.run_chat_agent([{"role": "user", "content": "hi"}],
                              session_id=None, cwd=".", complete_fn=_complete))
    return calls["n"]


def test_the_ceiling_holds_for_callers_with_no_session(monkeypatch):
    """The budget was measured as a delta of the SESSION's per-turn request
    count, so it was inert for every unattended caller — the paths where a
    retry storm has nobody watching it. One step could spend 72 generations
    against a declared ceiling of 6."""
    monkeypatch.setenv("AIFORGE_CHAT_LLM_RETRIES", "5")
    monkeypatch.setenv("AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP", "6")
    assert _run_unattended(gens_per_call=3) <= 8


def test_a_new_message_mid_step_does_not_refund_the_budget(monkeypatch):
    """The sweep sleeps between retries. Measured against the session's turn
    counter, a `turn_reset` from the user's NEXT message drove the spend
    negative and switched the abandoned step's ceiling off entirely."""
    monkeypatch.setenv("AIFORGE_CHAT_LLM_RETRIES", "5")
    monkeypatch.setenv("AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP", "4")
    sid = 92
    calls = {"n": 0}

    def _complete(*_a, **_k):
        calls["n"] += 1
        call_meter.record("chat", session_id=sid)
        if calls["n"] == 2:
            call_meter.turn_reset(sid)         # the user sent another message
        raise RuntimeError("llm.exhausted role=chat")

    list(_loop.run_chat_agent([{"role": "user", "content": "hi"}],
                              session_id=sid, cwd=".", complete_fn=_complete))
    assert calls["n"] <= 4


def test_unrelated_session_traffic_does_not_spend_the_step(monkeypatch):
    """A background fold billed to the same chat used to eat the step's
    generation budget: the sweep gave up after two tries because something
    else ran."""
    monkeypatch.setenv("AIFORGE_CHAT_LLM_RETRIES", "5")
    monkeypatch.setenv("AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP", "6")
    sid = 93
    calls = {"n": 0}

    def _fold():
        # A REAL background fold runs on its own thread, so it inherits no step
        # context — which is exactly why the step counter is immune to it and
        # the old session-counter measure was not.
        for _ in range(2):
            call_meter.record("learner", session_id=sid)

    def _complete(*_a, **_k):
        calls["n"] += 1
        call_meter.record("chat", session_id=sid)
        t = threading.Thread(target=_fold)
        t.start()
        t.join()
        raise RuntimeError("llm.exhausted role=chat")

    list(_loop.run_chat_agent([{"role": "user", "content": "hi"}],
                              session_id=sid, cwd=".", complete_fn=_complete))
    assert calls["n"] == 6, f"the step got {calls['n']} of its 6 generations"


def test_a_negative_ceiling_is_not_read_as_no_ceiling(monkeypatch):
    """-1 reads as "tighter than 0", but 0 means DISABLED — clamping a
    negative to 0 removed the bound the operator was trying to tighten."""
    monkeypatch.setenv("AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP", "-1")
    assert _loop._max_gen_per_step() == 6
