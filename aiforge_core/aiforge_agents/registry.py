"""Agent registry — pluggable archetype lookup.

Archetypes register via @register('name'). Build looks up by name +
applies the runtime config (model, temp, tools, prompt version).
This is the only place runtime.agent_runner asks for an agent.

Public:
    @register('planner')
    class Planner(BaseArchetype): ...

    agent = registry.build('planner', repo='X', ticket_id='Y')
"""
from __future__ import annotations

from typing import Callable

_REG: dict[str, type] = {}


def register(name: str) -> Callable[[type], type]:
    """Class decorator: bind name -> archetype class."""
    def _wrap(cls: type) -> type:
        if name in _REG:
            raise ValueError(f"archetype '{name}' already registered: {_REG[name]}")
        _REG[name] = cls
        return cls
    return _wrap


def build(name: str, **ctx):
    """Instantiate archetype `name` with ctx. KeyError if unknown."""
    if name not in _REG:
        raise KeyError(
            f"archetype '{name}' not registered. "
            f"known: {sorted(_REG)}"
        )
    return _REG[name](**ctx)


def known() -> list[str]:
    return sorted(_REG)
