"""Sanity tests for the per-archetype modules under aiforge_core.agents.*.

Each archetype file is mostly declarative (ROLE, PROMPT, OUTPUT_KEY,
TOOLS_FACTORY, build). The tests assert:

* every module exports the same surface
* ROLE matches an entry in agents.yaml
* PROMPT is non-empty for every archetype that has a system prompt
* TOOLS_FACTORY behaves consistently across modules
* the registry resolves every role to its module
"""
from __future__ import annotations

import pytest

from aiforge_core.agents import (
    ARCHETYPES,
    architect, doer, feedback, learner, planner,
    refiner, researcher, triage, verifier,
    load_agents,
)


ALL_MODULES = [
    architect, triage, planner, verifier, researcher,
    doer, refiner, feedback, learner,
]


@pytest.mark.parametrize("mod", ALL_MODULES, ids=lambda m: m.ROLE)
def test_module_exports_required_surface(mod):
    """Every archetype module must expose the same five attributes."""
    for attr in ("ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"):
        assert hasattr(mod, attr), f"{mod.__name__} is missing {attr}"


@pytest.mark.parametrize("mod", ALL_MODULES, ids=lambda m: m.ROLE)
def test_role_resolves_in_yaml(mod):
    contracts = load_agents()
    assert mod.ROLE in contracts


@pytest.mark.parametrize("mod", ALL_MODULES, ids=lambda m: m.ROLE)
def test_role_is_lowercase_and_non_empty(mod):
    assert mod.ROLE
    assert mod.ROLE == mod.ROLE.lower()


def test_prompts_non_empty_for_real_agents():
    """The architect is external (no prompt); every other agent owns one."""
    for mod in ALL_MODULES:
        if mod is architect:
            assert mod.PROMPT == ""
            continue
        assert isinstance(mod.PROMPT, str) and len(mod.PROMPT) > 50, (
            f"{mod.ROLE} prompt looks empty/stub")


def test_tools_factory_callable_or_none():
    for mod in ALL_MODULES:
        assert mod.TOOLS_FACTORY is None or callable(mod.TOOLS_FACTORY), (
            f"{mod.ROLE}.TOOLS_FACTORY is neither None nor callable")


def test_only_doer_and_researcher_have_tools():
    """Exact tool-bearing roles — guard against accidental tool grants."""
    with_tools = {m.ROLE for m in ALL_MODULES if m.TOOLS_FACTORY}
    assert with_tools == {"doer", "researcher"}


def test_architect_build_returns_none():
    """Architect is external — no ADK agent to construct."""
    assert architect.build(lambda role: None) is None


def test_archetype_registry_resolves_every_module():
    """ARCHETYPES dict-view must surface every per-archetype module."""
    expected_roles = {
        "architect", "triage", "planner", "verifier", "researcher",
        "doer", "refiner", "feedback", "learner",
    }
    assert set(ARCHETYPES.keys()) == expected_roles
    for role in expected_roles:
        mod = ARCHETYPES[role]
        assert mod.ROLE == role


def test_output_keys_unique_per_role_with_state():
    """ADK rejects two agents writing to the same session-state key.

    Architect has no output_key (external). All others must be unique.
    """
    keys = [m.OUTPUT_KEY for m in ALL_MODULES if m.OUTPUT_KEY]
    assert len(keys) == len(set(keys)), f"duplicate output_keys: {keys}"
