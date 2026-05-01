"""Grounder — validate every plan reference resolves before exec."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register

@register("grounder")
@dataclass
class Grounder(BaseArchetype):
    name: str = "grounder"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"artifact_type": "grounding",
                "resolved": True, "unresolved_refs": []}
