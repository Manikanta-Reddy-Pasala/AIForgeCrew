"""Architect — read-only review; opens MR if approved."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register

@register("architect")
@dataclass
class Architect(BaseArchetype):
    name: str = "architect"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"artifact_type": "review",
                "decision": "approve", "comments": [],
                "mr_title": "", "mr_body": ""}
