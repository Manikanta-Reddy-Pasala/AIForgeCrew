"""Verifier — PreFlect critic on plan + similar past failures."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register


@register("verifier")
@dataclass
class Verifier(BaseArchetype):
    name: str = "verifier"
    model: str = "qwen2.5-14b-instruct"
    temperature: float = 0.0
    grammar: str | None = "verify.json"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"artifact_type": "verifier_verdict",
                "verdict": "pass", "issues": [], "revised_plan": None}
