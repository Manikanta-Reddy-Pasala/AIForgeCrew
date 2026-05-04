"""Bundled default archetype config — read from
`aiforge_core/aiforge_agents/agents.defaults.yaml`.

Operators override via ~/.aiforge/agents.yaml or per-repo
.aiforge/agents.yaml. Defaults live with the code so the package
ships sensible values out of the box.
"""
from __future__ import annotations

from pathlib import Path

from aiforge_core.agents.config import ArchetypeConfig, _load_yaml


_DEFAULTS_PATH = Path(__file__).parent / "agents.defaults.yaml"


def load(name: str) -> ArchetypeConfig:
    data = _load_yaml(_DEFAULTS_PATH)
    arch = (data.get("archetypes") or {}).get(name) or {}
    return ArchetypeConfig(
        model=str(arch.get("model", "")),
        temperature=arch.get("temperature"),
        top_p=arch.get("top_p"),
        repetition_penalty=arch.get("repetition_penalty"),
        tools=list(arch.get("tools") or []),
        prompt_version=arch.get("prompt_version"),
        grammar=arch.get("grammar"),
        max_tokens=arch.get("max_tokens"),
    )
