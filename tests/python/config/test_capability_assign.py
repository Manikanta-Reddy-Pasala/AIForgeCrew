"""Capability-based role→model assignment: quick roles (enhancer/learner/chat)
must get the FAST non-thinking model, not a reasoning model (which returns empty
on short tasks). Deep roles (planner/reviewer) get the thinking model."""
from __future__ import annotations
import pytest

from aiforge_core.config import model_registry as mr


def _two_models(monkeypatch):
    monkeypatch.setattr(mr, "list_models", lambda: [
        {"id": "reason", "model": "reason", "has_thinking": True,
         "context_window": 128000, "has_vision": False},
        {"id": "fast", "model": "fast", "has_thinking": False,
         "context_window": 64000, "has_vision": False},
    ])


def test_quick_roles_get_fast_deep_roles_get_thinking(monkeypatch):
    _two_models(monkeypatch)
    p = mr.suggest_assignments(
        ["chat", "enhancer", "learner", "triage", "doer", "planner",
         "architect", "reviewer", "validator"])
    for quick in ("chat", "enhancer", "learner", "triage", "doer"):
        assert p[quick] == "fast", (quick, p[quick])
    for deep in ("planner", "architect", "reviewer", "validator"):
        assert p[deep] == "reason", (deep, p[deep])


def test_default_prefers_fast_over_reasoning(monkeypatch):
    _two_models(monkeypatch)
    # an unclassified role must NOT default to the reasoning model
    assert mr.suggest_assignments(["some_random_role"])["some_random_role"] == "fast"


def test_single_thinking_model_no_crash(monkeypatch):
    monkeypatch.setattr(mr, "list_models", lambda: [
        {"id": "reason", "model": "reason", "has_thinking": True,
         "context_window": 128000, "has_vision": False}])
    p = mr.suggest_assignments(["chat", "enhancer", "planner"])
    assert all(v == "reason" for v in p.values())      # falls back, no error
