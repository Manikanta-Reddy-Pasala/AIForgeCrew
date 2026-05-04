"""Coordinator — managed-agent wrapper for parallel Researcher + Doer."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.agents.base import BaseArchetype
from aiforge_core.agents.registry import register

@register("coordinator")
@dataclass
class Coordinator(BaseArchetype):
    name: str = "coordinator"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"artifact_type": "coordination",
                "managed": [], "merged_outcome": None}
