"""Planner — six-layer hardened plan generation."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register


@register("planner")
@dataclass
class Planner(BaseArchetype):
    name: str = "planner"
    model: str = "deepseek-r1-distill-32b"  # reasoning model
    temperature: float = 0.3
    top_p: float | None = 0.9
    grammar: str | None = "plan.gbnf"
    max_tokens: int = 6000

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        return {
            "artifact_type": "plan",
            "steps": [],
            "expected_token_budget": 0,
            "from_skill": None,
        }
