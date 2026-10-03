"""RetryPolicy reads today's env names with today's defaults and clamps, and
agrees with the leaves that read the same knobs themselves."""
import pytest

from aiforge_core.llm import model_wait, retry_policy
from aiforge_core.llm.client import _attempt, _http_retry

_ENV = ("AIFORGE_LLM_RETRY_MAX", "AIFORGE_LLM_EMPTY_RETRIES",
        "AIFORGE_CHAT_LLM_RETRIES", "AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP",
        "AIFORGE_LLM_WAIT_MAX_S", "AIFORGE_CHAT_PERSIST_S",
        "AIFORGE_CHAT_PERSIST_OTHER_S", "AIFORGE_PIPELINE_PERSIST_S",
        "AIFORGE_PIPELINE_PERSIST_OTHER_S", "AIFORGE_LLM_ATTEMPT_RETRIES",
        "AIFORGE_PRIMARY_DEMOTE_AFTER")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(model_wait, "_DEFAULT_MAX_S", 0.0)


def test_defaults_are_todays_numbers():
    p = retry_policy.RetryPolicy.from_env()
    assert (p.transport_attempts, p.empty_attempts, p.sweeps,
            p.generation_budget, p.wait_max_s, p.persist_s, p.persist_other_s,
            p.attempt_retries, p.demote_after) == (3, 3, 8, 10, 0.0, 0.0,
                                                   1800.0, 1, 2)
    q = retry_policy.RetryPolicy.from_env("pipeline")
    assert (q.persist_s, q.persist_other_s) == (0.0, 1800.0)


def test_each_surface_reads_its_own_persist_knobs(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PERSIST_S", "7")
    monkeypatch.setenv("AIFORGE_PIPELINE_PERSIST_S", "9")
    monkeypatch.setenv("AIFORGE_CHAT_PERSIST_OTHER_S", "0")
    monkeypatch.setenv("AIFORGE_PIPELINE_PERSIST_OTHER_S", "55")
    c = retry_policy.RetryPolicy.from_env("chat")
    p = retry_policy.RetryPolicy.from_env("pipeline")
    assert (c.persist_s, c.persist_other_s) == (7.0, 1.0)     # floor of 1 s
    assert (p.persist_s, p.persist_other_s) == (9.0, 55.0)


@pytest.mark.parametrize("raw,want", [("junk", 8), ("-3", 0), ("2", 2)])
def test_sweeps_clamp(monkeypatch, raw, want):
    monkeypatch.setenv("AIFORGE_CHAT_LLM_RETRIES", raw)
    assert retry_policy.RetryPolicy.from_env().sweeps == want


@pytest.mark.parametrize("raw,want", [("junk", 10), ("-1", 10), ("0", 0), ("4", 4)])
def test_generation_budget_clamp(monkeypatch, raw, want):
    monkeypatch.setenv("AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP", raw)
    assert retry_policy.RetryPolicy.from_env().generation_budget == want


@pytest.mark.parametrize("raw", [None, "1", "0", "-2", "junk", "7"])
def test_leaf_knobs_match_the_leaves(monkeypatch, raw):
    if raw is not None:
        monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", raw)
        monkeypatch.setenv("AIFORGE_LLM_EMPTY_RETRIES", raw)
    p = retry_policy.RetryPolicy.from_env()
    assert p.transport_attempts == _http_retry._RetryCfg(60).max_attempts
    assert p.empty_attempts == max(0, _attempt._int_env("AIFORGE_LLM_EMPTY_RETRIES", 3))


def test_wait_bound_is_the_shared_knob(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_WAIT_MAX_S", "600")
    assert retry_policy.RetryPolicy.from_env().wait_max_s == model_wait.wait_max_s() == 600


def test_attempt_backoff():
    assert [retry_policy.attempt_backoff_s(t) for t in range(6)] == [
        0.6, 1.1, 2.1, 4.1, 8.1, 8.1]


def test_the_policy_is_frozen():
    with pytest.raises(Exception):
        retry_policy.RetryPolicy.from_env().sweeps = 1
