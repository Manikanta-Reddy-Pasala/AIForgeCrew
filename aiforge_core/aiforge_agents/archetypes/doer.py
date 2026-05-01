"""Doer — CRITIC loop with five layered checks."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register

@register("doer")
@dataclass
class Doer(BaseArchetype):
    name: str = "doer"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"artifact_type": "doer_outcome",
                "diffs": [], "applied": False, "tests_green": False}
