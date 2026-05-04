"""Circuit-breaker behaviour."""
from __future__ import annotations

import time

from aiforge_core.orchestrator.circuit_breakers import CircuitBreakers


def test_starts_untripped() -> None:
    cb = CircuitBreakers()
    assert cb.tripped is False


def test_token_budget_trips() -> None:
    cb = CircuitBreakers(token_budget_multiplier=2.0)
    cb.check_token_budget("s1", used=2500, expected=1000)
    assert cb.tripped is True
    assert "token_budget" in cb.state.reason


def test_token_budget_below_threshold() -> None:
    cb = CircuitBreakers(token_budget_multiplier=2.0)
    cb.check_token_budget("s1", used=1500, expected=1000)
    assert cb.tripped is False


def test_retries_per_step_trips() -> None:
    cb = CircuitBreakers(retries_per_step_max=3)
    for _ in range(4):
        cb.record_retry("s1")
    assert cb.tripped is True


def test_audit_failure_immediate_trip() -> None:
    cb = CircuitBreakers(audit_failure_max=1)
    cb.record_audit_failure()
    assert cb.tripped is True


def test_first_trip_wins_reason_sticky() -> None:
    cb = CircuitBreakers()
    cb.trip("a", "first")
    cb.trip("b", "second")
    assert "first" in cb.state.reason
