"""Understander — read ticket + AiForgeMemory; produce Understanding artifact."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register


@register("understander")
@dataclass
class Understander(BaseArchetype):
    name: str = "understander"
    model: str = "qwen2.5-14b-instruct"
    temperature: float = 0.3
    tools: list[str] = field(default_factory=list)  # populated at build-time from runtime.tool_registry
    prompt_version: str = "v1"
    grammar: str | None = "understanding.json"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        # Stub — wired to ADK LlmAgent in runtime.agent_runner.
        return {
            "artifact_type": "understanding",
            "problem": "",
            "knowns": [],
            "unknowns": [],
            "risks": [],
            "ambiguities": [],
        }
