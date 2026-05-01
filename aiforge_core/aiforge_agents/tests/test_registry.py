"""Registry + archetype-binding tests."""
from __future__ import annotations

from aiforge_core.aiforge_agents import registry
import aiforge_core.aiforge_agents.archetypes  # noqa: F401  (triggers @register)


def test_all_archetypes_registered() -> None:
    expected = {
        "understander", "planner", "verifier", "grounder",
        "doer", "tester", "architect", "coordinator", "learner",
    }
    assert expected.issubset(set(registry.known()))


def test_build_planner_returns_correct_class() -> None:
    p = registry.build("planner")
    assert p.name == "planner"
    assert p.grammar == "plan.gbnf"
    assert p.temperature == 0.3


def test_build_doer_uses_coder_grammar() -> None:
    d = registry.build("doer")
    assert d.grammar == "udiff.gbnf"
    assert d.temperature == 0.2
    assert d.repetition_penalty == 1.05


def test_build_unknown_raises() -> None:
    import pytest
    with pytest.raises(KeyError):
        registry.build("nonexistent")
