"""Learner — ADK after_model + after_tool callback. Online + offline modes."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register


@register("learner")
@dataclass
class Learner(BaseArchetype):
    name: str = "learner"
    model: str = ""  # often non-LLM (heuristic) or qwen2.5-14b for distillation

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"artifact_type": "learning",
                "step_traces_written": 0,
                "skills_promoted": [], "patterns_updated": []}
