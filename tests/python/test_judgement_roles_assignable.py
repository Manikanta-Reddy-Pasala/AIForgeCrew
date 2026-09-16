"""A judgement role has to be ASSIGNABLE, or the reasoning tier never reaches it.

identity.model in agents.yaml is documentation only. A role missing from the
archetype list resolves to the global default, cannot be picked in Settings and
is skipped by auto-assign — so `memory` (distillation) and `validator` (the
final verdict) always ran on whatever the box's default model was, whatever
model_registry said they should use.
"""
from __future__ import annotations

import pytest

from aiforge_core.config import agent_config, model_registry


@pytest.mark.parametrize("role", ["memory", "validator"])
def test_the_role_is_configurable(role):
    assert role in agent_config.archetypes()


@pytest.mark.parametrize("role", ["memory", "validator"])
def test_the_role_is_a_thinking_role_so_auto_assign_routes_it(role):
    assert not model_registry.is_fast_role(role)
    assert any(t in role for t in model_registry._THINKING_ROLES)


def test_auto_assign_gives_memory_the_reasoning_model(monkeypatch):
    # the shape suggest_assignments actually reads
    models = [
        {"id": "fast-coder", "model": "fast-coder", "has_thinking": False,
         "has_vision": False, "context_window": 128000},
        {"id": "deep-thinker", "model": "deep-thinker", "has_thinking": True,
         "has_vision": False, "context_window": 128000},
    ]
    monkeypatch.setattr(model_registry, "list_models", lambda: models)
    plan = model_registry.suggest_assignments(["memory", "learner"])
    assert plan.get("memory") == "deep-thinker", plan
    assert plan.get("learner") != "deep-thinker", plan


def test_every_agents_yaml_judge_that_wants_reasoning_is_assignable():
    """Guard against the same gap reopening for the next role someone adds."""
    import yaml
    from pathlib import Path

    import aiforge_core.agents as agents_pkg

    shipped = yaml.safe_load(
        (Path(agents_pkg.__file__).parent / "agents.yaml").read_text())["agents"]
    roster = set(agent_config.archetypes())
    wants_reasoning = [r for r in shipped
                       if not model_registry.is_fast_role(r)
                       and any(t in r for t in model_registry._THINKING_ROLES)
                       and shipped[r]["identity"].get("runtime") != "external_operator"]
    missing = [r for r in wants_reasoning if r not in roster]
    assert not missing, f"thinking roles that can never be given a model: {missing}"
