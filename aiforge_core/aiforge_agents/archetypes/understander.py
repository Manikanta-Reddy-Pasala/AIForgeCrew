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
