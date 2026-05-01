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


def test_build_planner_picks_up_defaults() -> None:
    p = registry.build("planner")
    assert p.name == "planner"
    assert p.model == "deepseek-r1-distill-32b"
    assert p.grammar == "plan.gbnf"
    assert p.temperature == 0.3


def test_build_doer_picks_up_defaults() -> None:
    d = registry.build("doer")
    assert d.model == "qwen3-coder-next"
    assert d.grammar == "udiff.gbnf"
    assert d.temperature == 0.2
    assert d.repetition_penalty == 1.05


def test_build_unknown_raises() -> None:
    import pytest
    with pytest.raises(KeyError):
        registry.build("nonexistent")


def test_per_repo_yaml_overrides_defaults(tmp_path) -> None:
    (tmp_path / ".aiforge").mkdir()
    (tmp_path / ".aiforge" / "agents.yaml").write_text(
        "archetypes:\n"
        "  planner:\n"
        "    model: my-custom-llm\n"
        "    temperature: 0.7\n"
    )
    p = registry.build("planner", repo_path=tmp_path)
    assert p.model == "my-custom-llm"
    assert p.temperature == 0.7
    # Unaffected fields keep defaults
    assert p.grammar == "plan.gbnf"


def test_global_yaml_used_when_no_repo_override(tmp_path, monkeypatch) -> None:
    g = tmp_path / "agents.yaml"
    g.write_text(
        "archetypes:\n"
        "  doer:\n"
        "    model: global-doer-model\n"
    )
    monkeypatch.setenv("AIFORGE_AGENTS_CONFIG", str(g))
    import importlib
    from aiforge_core.aiforge_agents import config as agent_cfg
    importlib.reload(agent_cfg)
    d = registry.build("doer")
    assert d.model == "global-doer-model"
