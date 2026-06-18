"""gap_eval archetype registration + contract tests. No ADK import."""
from __future__ import annotations

from aiforge_core.agents import _base
from aiforge_core.config import agent_config


def test_gap_eval_in_archetypes() -> None:
    assert "gap_eval" in agent_config.archetypes()


def test_gap_eval_contract_loads() -> None:
    c = _base.contract_for("gap_eval")               # raises KeyError if missing
    assert c.contract.max_wall_s > 0
    assert "file_write" in (c.tools.forbidden or [])  # read-only judge
