"""Change 4 — local-primary same-endpoint retries default to 1.

3× same-endpoint read-retries on a transient error burns serial minutes on a
LOCAL primary before escalating to cloud. The connect-preflight already
fails-fast on unreachable hosts, so the retries are pure read-retry latency.
Default flips 3 → 1 (one try, then escalate); env override preserved.
"""
from __future__ import annotations

from aiforge_core.runtime import escalating_llm as el


def test_attempt_retries_default_is_one(monkeypatch):
    monkeypatch.delenv("AIFORGE_LLM_ATTEMPT_RETRIES", raising=False)
    assert el._attempt_retries() == 1


def test_attempt_retries_env_override(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_ATTEMPT_RETRIES", "4")
    assert el._attempt_retries() == 4


def test_attempt_retries_floor_is_one(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_ATTEMPT_RETRIES", "0")
    assert el._attempt_retries() == 1
    monkeypatch.setenv("AIFORGE_LLM_ATTEMPT_RETRIES", "bogus")
    assert el._attempt_retries() == 1
