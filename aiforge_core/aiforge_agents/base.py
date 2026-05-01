"""BaseArchetype — common shape every agent class implements.

Subclasses populate role-specific fields:
    name (str)              — registered name
    model (str)             — inference model id
    temperature (float)
    tools (list[str])       — names from tool_registry
    prompt_version (str)    — prompt registry version
    grammar (str | None)    — GBNF/JSON-schema name (if structured output)

Methods:
    run(self, *, ticket_id, ctx) -> ArtifactDict
        Subclass overrides; default returns NotImplemented.

The base does NOT touch ADK directly — runtime.agent_runner wraps
this into an ADK LlmAgent. Keeps archetypes pure data + role logic,
testable without ADK.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BaseArchetype:
    name: str = ""
    model: str = ""
    temperature: float = 0.0
    top_p: float | None = None
    repetition_penalty: float | None = None
    tools: list[str] = field(default_factory=list)
    prompt_version: str = "v1"
    grammar: str | None = None
    max_tokens: int = 4096
    repo: str = ""
    ticket_id: str = ""

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            f"{type(self).__name__}.run() must be overridden"
        )
