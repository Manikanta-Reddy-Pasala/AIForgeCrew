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


def build(name: str, *, repo_path=None, **ctx):
    """Instantiate archetype `name`. Pulls model/sampling/grammar from
    config (per-repo agents.yaml > global ~/.aiforge/agents.yaml >
    bundled defaults). KeyError if unknown."""
    if name not in _REG:
        raise KeyError(
            f"archetype '{name}' not registered. "
            f"known: {sorted(_REG)}"
        )
    inst = _REG[name](**ctx)
    # Layered config: defaults < global < per-repo override.
    from aiforge_core.aiforge_agents import config as agent_cfg
    from aiforge_core.aiforge_agents import defaults as agent_defaults
    agent_defaults.load(name).merge_into(inst)
    agent_cfg.load(name, repo_path=repo_path).merge_into(inst)
    return inst


def known() -> list[str]:
    return sorted(_REG)
