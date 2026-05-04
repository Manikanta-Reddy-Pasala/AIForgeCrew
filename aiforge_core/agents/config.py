"""Per-archetype configuration — `~/.aiforge/agents.yaml` (global)
or `<repo>/.aiforge/agents.yaml` (per-repo override).

Schema:

    archetypes:
      planner:
        model: deepseek-r1-distill-32b
        temperature: 0.3
        top_p: 0.9
        grammar: plan.gbnf
        max_tokens: 6000
        prompt_version: v1
        tools: [read_file, related_memories]
      doer:
        model: qwen3-coder-next
        temperature: 0.2
        top_p: 0.95
        repetition_penalty: 1.05
        grammar: udiff.gbnf
        max_tokens: 8000
      ...

Lookup order:
    1. Per-call kwargs to registry.build(...)
    2. Per-repo .aiforge/agents.yaml at given repo_path
    3. Global ~/.aiforge/agents.yaml
    4. Hard-coded class defaults (BaseArchetype)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore


GLOBAL_PATH = Path(os.environ.get(
    "AIFORGE_AGENTS_CONFIG",
    os.path.expanduser("~/.aiforge/agents.yaml"),
))


@dataclass
class ArchetypeConfig:
    model: str = ""
    temperature: float | None = None
    top_p: float | None = None
    repetition_penalty: float | None = None
    tools: list[str] = field(default_factory=list)
    prompt_version: str | None = None
    grammar: str | None = None
    max_tokens: int | None = None

    def merge_into(self, archetype) -> None:
        """Apply non-None fields onto a built archetype instance."""
        if self.model:
            archetype.model = self.model
        if self.temperature is not None:
            archetype.temperature = self.temperature
        if self.top_p is not None:
            archetype.top_p = self.top_p
        if self.repetition_penalty is not None:
            archetype.repetition_penalty = self.repetition_penalty
        if self.tools:
            archetype.tools = list(self.tools)
        if self.prompt_version is not None:
            archetype.prompt_version = self.prompt_version
        if self.grammar is not None:
            archetype.grammar = self.grammar
        if self.max_tokens is not None:
            archetype.max_tokens = self.max_tokens


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.is_file():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}


def load(name: str, *, repo_path: str | Path | None = None) -> ArchetypeConfig:
    """Resolve archetype config (per-repo override > global).

    `name` is the registered archetype name (e.g. "planner", "doer").
    """
    # Per-repo override wins
    repo_data: dict[str, Any] = {}
    if repo_path:
        repo_yaml = Path(repo_path) / ".aiforge" / "agents.yaml"
        repo_data = _load_yaml(repo_yaml)

    global_data = _load_yaml(GLOBAL_PATH)

    archs_repo   = (repo_data.get("archetypes") or {}).get(name) or {}
    archs_global = (global_data.get("archetypes") or {}).get(name) or {}

    merged = {**archs_global, **archs_repo}   # repo wins
    return ArchetypeConfig(
        model=str(merged.get("model", "")),
        temperature=merged.get("temperature"),
        top_p=merged.get("top_p"),
        repetition_penalty=merged.get("repetition_penalty"),
        tools=list(merged.get("tools") or []),
        prompt_version=merged.get("prompt_version"),
        grammar=merged.get("grammar"),
        max_tokens=merged.get("max_tokens"),
    )
