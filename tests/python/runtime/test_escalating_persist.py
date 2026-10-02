"""A pipeline model stage that keeps failing is retried (waiting) until it
answers; only a failure waiting cannot fix, or a bound, ends it."""
import time

import pytest

from aiforge_core.runtime.escalating_llm import _wrapper as W


class _Fake:
    role = "doer"
    _persist_gap = W.EscalatingLlm._persist_gap


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AIFORGE_PIPELINE_PERSIST_S", "0")


def test_a_generic_failure_waits_and_retries_with_growing_gaps():
    f, t0 = _Fake(), time.monotonic()
    gaps = [f._persist_gap(RuntimeError("HTTP 500"), n, t0) for n in range(6)]
    assert gaps == [5.0, 10.0, 20.0, 30.0, 30.0, 30.0]


def test_an_empty_answer_chain_is_retried_too():
    assert _Fake()._persist_gap(None, 0, time.monotonic()) == 5.0


def test_off_and_bound(monkeypatch):
    f = _Fake()
    monkeypatch.setenv("AIFORGE_PIPELINE_PERSIST_S", "-1")
    assert f._persist_gap(RuntimeError("x"), 0, time.monotonic()) is None
    monkeypatch.setenv("AIFORGE_PIPELINE_PERSIST_S", "3")
    assert f._persist_gap(RuntimeError("x"), 0, time.monotonic()) is None


def test_a_config_error_or_llm_issue_is_not_retried(monkeypatch):
    from aiforge_core.llm import model_outage as mo
    f = _Fake()
    monkeypatch.setattr(mo, "classify", lambda e: mo.CONFIG)
    assert f._persist_gap(RuntimeError("401"), 0, time.monotonic()) is None
    monkeypatch.setattr(mo, "classify", lambda e: mo.OTHER)
    monkeypatch.setattr(mo, "issue", lambda e: object())
    assert f._persist_gap(RuntimeError("x"), 0, time.monotonic()) is None
