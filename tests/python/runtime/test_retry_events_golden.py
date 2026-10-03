"""The model-call recovery emits exactly these events, in this order.

Characterization of ``_completion._retry_completion`` (sweep, outage wait,
persist rounds, overflow shrink, give-up): the events and the outcome of each
scenario are pinned in ``golden_retry_events.json``. A refactor of the retry
code (llm/retry_policy) must leave every line unchanged.

Regenerate only on purpose: ``AIFORGE_WRITE_GOLDEN=1 pytest <this file>``.
"""
import json
import os
from pathlib import Path

import pytest

from aiforge_core.llm import model_outage, model_wait
from aiforge_core.runtime import chat_cancel, run_interrupt
from aiforge_core.runtime.chat_agent._turn import _completion as C

GOLDEN = Path(__file__).with_name("golden_retry_events.json")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    for k in ("AIFORGE_CHAT_LLM_RETRIES", "AIFORGE_CHAT_PERSIST_S",
              "AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP",
              "AIFORGE_CHAT_PERSIST_OTHER_S", "AIFORGE_LLM_WAIT_MAX_S"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(run_interrupt, "pause", lambda *a, **k: None)
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    monkeypatch.setattr(model_wait, "cancel_reason", lambda: None)
    monkeypatch.setattr(model_wait, "_SCHEDULE", (0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(model_wait, "probe_max_s", lambda: 0.0)
    monkeypatch.setattr(C, "_shrink_for_retry", lambda *a: False)


def _drive(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


def _flaky(fails, exc):
    left = [fails]
    calls = []

    def fn(role, convo):
        calls.append(1)
        if left[0] > 0:
            left[0] -= 1
            raise exc
        return "FINAL: back"
    return fn


def _name(out):
    for n in ("_RETRY_STOP", "_CANCELLED", "_STEERED"):
        if out is getattr(C, n):
            return n
    return out


def _run(fn, exc, **kw):
    kw.setdefault("wait_s", None)
    events, out = _drive(C._retry_completion(
        fn, "chat", [{"role": "system", "content": "s"}], kw.pop("sid", None),
        exc, kw.pop("step_calls", None), None, None, **kw))
    return {"events": events, "out": _name(out)}


def _s_sweep_recovers(mp):
    mp.setenv("AIFORGE_CHAT_LLM_RETRIES", "4")
    exc = RuntimeError("HTTP 500 boom")
    return _run(_flaky(2, exc), exc)


def _s_sweep_exhausted_no_persist(mp):
    mp.setenv("AIFORGE_CHAT_LLM_RETRIES", "2")
    mp.setenv("AIFORGE_CHAT_PERSIST_S", "-1")
    exc = RuntimeError("HTTP 500 boom")
    return _run(_flaky(99, exc), exc)


def _s_sweep_exhausted_worked(mp):
    mp.setenv("AIFORGE_CHAT_LLM_RETRIES", "1")
    mp.setenv("AIFORGE_CHAT_PERSIST_S", "-1")
    exc = RuntimeError("HTTP 500 boom")
    return _run(_flaky(99, exc), exc, worked=True)


def _s_sweep_then_persist(mp):
    mp.setenv("AIFORGE_CHAT_LLM_RETRIES", "2")
    mp.setattr(C, "_shrink_for_retry", lambda *a: True)
    exc = RuntimeError("HTTP 500 boom")
    return _run(_flaky(5, exc), exc)


def _s_persist_bound(mp):
    mp.setenv("AIFORGE_CHAT_LLM_RETRIES", "0")
    mp.setenv("AIFORGE_CHAT_PERSIST_S", "0.0001")
    exc = RuntimeError("HTTP 500 boom")
    return _run(_flaky(99, exc), exc)


def _s_budget_caps_sweep(mp):
    mp.setenv("AIFORGE_CHAT_LLM_RETRIES", "5")
    mp.setenv("AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP", "3")
    mp.setenv("AIFORGE_CHAT_PERSIST_S", "-1")
    exc = RuntimeError("HTTP 500 boom")
    counter = {"n": 1}

    def fn(role, convo):
        counter["n"] += 1
        raise exc
    return _run(fn, exc, step_calls=counter)


def _s_outage_waits_then_answers(mp):
    exc = ConnectionRefusedError("refused")
    return _run(_flaky(6, exc), exc, wait_s=0)


def _s_outage_bounded_gives_up(mp):
    mp.setenv("AIFORGE_CHAT_PERSIST_S", "-1")
    mp.setattr(model_wait, "_SCHEDULE", (2.0, 5.0, 10.0, 30.0))
    mp.setattr(model_wait, "probe_max_s", lambda: 30.0)
    exc = ConnectionRefusedError("refused")
    return _run(_flaky(99, exc), exc, wait_s=20)


def _s_llm_issue_persist_then_answer(mp):
    exc = RuntimeError("crashes on this")
    mp.setattr(model_outage, "issue",
               lambda e: "the server crashes on it" if e is exc else None)
    return _run(_flaky(2, RuntimeError("other")), exc)


def _s_llm_issue_unpersisted(mp):
    mp.setenv("AIFORGE_CHAT_PERSIST_S", "-1")
    exc = RuntimeError("crashes on this")
    mp.setattr(model_outage, "issue", lambda e: "the server crashes on it")
    return _run(_flaky(9, exc), exc)


def _s_unserved_model(mp):
    from aiforge_core.llm import client
    mp.setenv("AIFORGE_CHAT_PERSIST_S", "-1")
    mp.setattr(client, "model_missing", lambda e: True)
    exc = RuntimeError("provider — model 'x' is not served here")
    return _run(_flaky(9, exc), exc)


def _s_overflow_twice_shrinks(mp):
    mp.setenv("AIFORGE_CHAT_LLM_RETRIES", "4")
    mp.setattr(model_outage, "is_context_overflow",
               lambda e: "too long" in str(e))
    mp.setattr(C, "_shrink_after_overflow", lambda *a: "restarted from a handoff")
    exc = RuntimeError("prompt too long")
    return _run(_flaky(3, exc), exc)


def _s_stop_during_sweep(mp):
    mp.setenv("AIFORGE_CHAT_LLM_RETRIES", "4")
    mp.setattr(run_interrupt, "pause", lambda *a, **k: "stop")
    exc = RuntimeError("HTTP 500 boom")
    return _run(_flaky(99, exc), exc)


def _s_steer_during_sweep(mp):
    mp.setenv("AIFORGE_CHAT_LLM_RETRIES", "4")
    mp.setattr(run_interrupt, "pause", lambda *a, **k: "steer")
    exc = RuntimeError("HTTP 500 boom")
    return _run(_flaky(99, exc), exc)


def _s_stop_flag_before_sweep(mp):
    mp.setenv("AIFORGE_CHAT_LLM_RETRIES", "4")
    mp.setattr(chat_cancel, "is_cancelled", lambda sid: True)
    exc = RuntimeError("HTTP 500 boom")
    return _run(_flaky(99, exc), exc, sid=5)


def _s_worker_stop_in_persist(mp):
    mp.setenv("AIFORGE_CHAT_LLM_RETRIES", "0")
    mp.setattr(model_wait, "cancel_reason", lambda: "stop")
    exc = RuntimeError("HTTP 500 boom")
    return _run(_flaky(99, exc), exc)


def _s_wait_steered(mp):
    mp.setattr(run_interrupt, "pause", lambda *a, **k: "steer")
    exc = ConnectionRefusedError("refused")
    return _run(_flaky(99, exc), exc, wait_s=0)


SCENARIOS = {f[3:]: f for f in globals() if f.startswith("_s_")}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_is_unchanged(name, monkeypatch):
    got = globals()[SCENARIOS[name]](monkeypatch)
    got = json.loads(json.dumps(got))
    if os.environ.get("AIFORGE_WRITE_GOLDEN"):
        data = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        data[name] = got
        GOLDEN.write_text(json.dumps(data, indent=1, ensure_ascii=False,
                                     sort_keys=True) + "\n")
        return
    assert got == json.loads(GOLDEN.read_text())[name]


def _gap_grid(monkeypatch):
    """The pipeline's persist decision over a grid of failures, rounds, elapsed
    time and settings (None = give up)."""
    import time

    from aiforge_core.runtime.escalating_llm import _wrapper as W
    excs = {"none": None, "500": RuntimeError("HTTP 500"),
            "401": RuntimeError("401 Unauthorized"),
            "refused": ConnectionRefusedError("refused"),
            "timeout": TimeoutError("timed out")}
    out = {}
    fake = type("F", (), {})()
    for persist in ("-1", "0", "3", "100"):
        for other in ("1800", "1"):
            monkeypatch.setenv("AIFORGE_PIPELINE_PERSIST_S", persist)
            monkeypatch.setenv("AIFORGE_PIPELINE_PERSIST_OTHER_S", other)
            for ename, exc in excs.items():
                for rounds in range(5):
                    for elapsed in (0.0, 50.0, 2000.0):
                        t0 = time.monotonic() - elapsed
                        out[f"{persist}/{other}/{ename}/{rounds}/{elapsed}"] = \
                            W.EscalatingLlm._persist_gap(fake, exc, rounds, t0)
    return out


def test_pipeline_persist_gap_is_unchanged(monkeypatch):
    got = json.loads(json.dumps(_gap_grid(monkeypatch)))
    if os.environ.get("AIFORGE_WRITE_GOLDEN"):
        data = json.loads(GOLDEN.read_text())
        data["pipeline_gap_grid"] = got
        GOLDEN.write_text(json.dumps(data, indent=1, ensure_ascii=False,
                                     sort_keys=True) + "\n")
        return
    assert got == json.loads(GOLDEN.read_text())["pipeline_gap_grid"]
