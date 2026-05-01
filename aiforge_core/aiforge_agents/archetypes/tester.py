"""Tester — TDD-first; writes failing tests before Doer iterates."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register

@register("tester")
@dataclass
class Tester(BaseArchetype):
    name: str = "tester"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"artifact_type": "test_plan",
                "tests": [], "coverage_target": 0.8}
