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
    model: str = "qwen3-coder-next"
    temperature: float = 0.2
    top_p: float | None = 0.95
    repetition_penalty: float | None = 1.05
    grammar: str | None = "udiff.gbnf"
    max_tokens: int = 8000

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"artifact_type": "doer_outcome",
                "diffs": [], "applied": False, "tests_green": False}
