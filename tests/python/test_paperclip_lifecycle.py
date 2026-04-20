"""Lifecycle v4.1 — parent/child two-SM model."""
from __future__ import annotations
import pytest

from aiforge_core.lifecycle import (
    parent_allowed_next, child_allowed_next, LifecycleError,
    parent_transitions, child_transitions,
)


def test_parent_sm_path():
    assert "planning" in parent_allowed_next("created")
    assert "splitting" in parent_allowed_next("planning")
    assert "spawned" in parent_allowed_next("splitting")
    assert "reflection" in parent_allowed_next("spawned")
    assert "closed" in parent_allowed_next("reflection")


def test_child_sm_path():
    assert "coding" in child_allowed_next("created")
    assert "reviewing" in child_allowed_next("coding")
    assert "mr_created" in child_allowed_next("reviewing")
    assert "coding" in child_allowed_next("reviewing")  # reject loop
    assert "merged" in child_allowed_next("mr_created")
    assert "escalated" in child_allowed_next("reviewing")


def test_invalid_transition_raises():
    with pytest.raises(LifecycleError):
        parent_allowed_next("nonexistent_state")
    assert "merged" not in parent_allowed_next("created")
    assert "reviewing" not in parent_allowed_next("created")
