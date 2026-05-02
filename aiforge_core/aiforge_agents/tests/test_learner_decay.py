"""Tests for skill/failure ranking decay (#10).

We can't easily round-trip Postgres in unit tests, so we cover the
exposed pure-python helpers (_decay_params + _decay_factor) — the SQL
mirrors that math via `power(0.5, age_days / half_life)`. Behaviour
contract: half-life N days means an N-day-old entry weighs half a fresh one.
"""
from __future__ import annotations

import math

import pytest

from aiforge_core.aiforge_agents.learner import online as learner


def test_decay_params_defaults(monkeypatch):
    monkeypatch.delenv("AIFORGE_LEARNER_HALFLIFE_DAYS", raising=False)
    monkeypatch.delenv("AIFORGE_LEARNER_CUTOFF_DAYS", raising=False)
    half, cutoff = learner._decay_params()
    assert half == 30.0
    assert cutoff == 180.0


def test_decay_params_env_override(monkeypatch):
    monkeypatch.setenv("AIFORGE_LEARNER_HALFLIFE_DAYS", "7")
    monkeypatch.setenv("AIFORGE_LEARNER_CUTOFF_DAYS", "60")
    half, cutoff = learner._decay_params()
    assert half == 7.0
    assert cutoff == 60.0


def test_decay_params_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("AIFORGE_LEARNER_HALFLIFE_DAYS", "abc")
    monkeypatch.setenv("AIFORGE_LEARNER_CUTOFF_DAYS", "xyz")
    half, cutoff = learner._decay_params()
    assert half == 30.0
    assert cutoff == 180.0


def test_decay_params_clamps_zero_halflife(monkeypatch):
    monkeypatch.setenv("AIFORGE_LEARNER_HALFLIFE_DAYS", "0")
    half, cutoff = learner._decay_params()
    assert half >= 0.5  # floor


def test_decay_factor_fresh_is_one():
    assert learner._decay_factor(0, 30) == 1.0


def test_decay_factor_halflife_is_half():
    """30-day-old entry with 30-day half-life weighs 0.5."""
    one_day = 86400
    val = learner._decay_factor(30 * one_day, 30)
    assert math.isclose(val, 0.5, rel_tol=1e-9)


def test_decay_factor_two_halflives():
    one_day = 86400
    val = learner._decay_factor(60 * one_day, 30)
    assert math.isclose(val, 0.25, rel_tol=1e-9)


def test_decay_factor_negative_age_clamped():
    """Future timestamps shouldn't blow up — treat as fresh."""
    assert learner._decay_factor(-1000, 30) == 1.0


def test_decay_factor_zero_halflife_safe():
    assert learner._decay_factor(86400, 0) == 1.0


def test_decay_old_outweighed_by_recent():
    """Recent low seen_count beats stale high seen_count under decay.

    seen_count=10 from 90d ago vs seen_count=3 fresh:
      old:  10 * 0.5^(90/30) = 10 * 0.125 = 1.25
      new:   3 * 1.0          = 3.0      <-- wins
    """
    one_day = 86400
    old_score = 10 * learner._decay_factor(90 * one_day, 30)
    fresh_score = 3 * learner._decay_factor(0, 30)
    assert fresh_score > old_score
