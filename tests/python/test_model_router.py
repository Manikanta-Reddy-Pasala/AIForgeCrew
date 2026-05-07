"""Tests for ``aiforge_core.runtime.model_router``."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import model_router as mr


def test_pick_doer_trivial_lands_on_lowest_tier():
    d = mr.pick("doer", "trivial")
    assert d.tier_index == 0
    assert d.model == mr.DOER_TIERS[0]


def test_pick_doer_moderate_lands_on_default():
    d = mr.pick("doer", "moderate")
    assert d.tier_index == 1
    assert d.model == mr.DOER_TIERS[1]


def test_pick_doer_hard_lands_on_top():
    d = mr.pick("doer", "hard")
    assert d.tier_index == len(mr.DOER_TIERS) - 1
    assert d.model == mr.DOER_TIERS[-1]


def test_pick_unknown_role_returns_empty_model():
    d = mr.pick("noexist", "moderate")
    assert d.model == ""
    assert d.tier_index == -1


def test_pick_unknown_complexity_falls_to_default():
    """Bad complexity strings shouldn't crash; default to mid tier."""
    d = mr.pick("doer", "wonky")
    assert d.model == mr.DOER_TIERS[1]


def test_pick_researcher_two_tiers():
    """Researcher has 2 tiers — moderate and hard both land on idx 1."""
    assert mr.pick("researcher", "trivial").tier_index == 0
    assert mr.pick("researcher", "moderate").tier_index == 1
    assert mr.pick("researcher", "hard").tier_index == 1


def test_pick_refiner_single_tier():
    d = mr.pick("refiner", "hard")
    assert d.tier_index == 0
    assert d.model == mr.REFINER_TIERS[0]


def test_next_doer_after_fail_climbs_one_step():
    nxt = mr.next_doer_model_after_fail(mr.DOER_TIERS[0])
    assert nxt == mr.DOER_TIERS[1]


def test_next_doer_after_fail_at_top_returns_none():
    assert mr.next_doer_model_after_fail(mr.DOER_TIERS[-1]) is None


def test_next_doer_after_fail_unknown_jumps_to_top():
    assert mr.next_doer_model_after_fail("some-rando-model") == mr.DOER_TIERS[-1]
