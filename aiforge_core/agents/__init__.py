"""Agent contract loader + per-archetype module registry.

Re-exports the YAML-driven contract API plus a thin registry that
resolves a role name to its per-archetype module. Each archetype lives
in its own file under this package so role-specific edits don't touch
the shared loader/yaml.

    from aiforge_core.agents import AgentContract, load_agents
    from aiforge_core.agents import doer, planner, refiner   # per-role modules

    # role-name -> module (lazy via :data:`ARCHETYPES`)
    from aiforge_core.agents import ARCHETYPES
    ARCHETYPES["doer"].build(model_factory)
"""
from __future__ import annotations

from .loader import (  # noqa: F401
    AgentContract,
    AgentSpecError,
    load_agents,
    tools_schema_for_role,
    validate_contracts,
)


def _archetypes() -> dict:
    """Lazy import to avoid circular imports during package load.

    Each role's module imports ``runtime.prompts`` etc., which in turn
    import other parts of ``aiforge_core``; eager-loading them at
    package import time has historically caused circular imports in
    the runtime layer. Lazy resolution keeps the API simple without
    paying that cost.
    """
    from . import (
        architect, doer, feedback, learner, planner,
        refiner, researcher, triage, verifier,
    )
    return {
        "architect":  architect,
        "triage":     triage,
        "planner":    planner,
        "verifier":   verifier,
        "researcher": researcher,
        "doer":       doer,
        "refiner":    refiner,
        "feedback":   feedback,
        "learner":    learner,
    }


class _ArchetypeRegistry(dict):
    """Dict-like view that lazily populates on first access."""

    def _hydrate(self) -> None:
        if not super().__len__():
            self.update(_archetypes())

    def __getitem__(self, key):  # type: ignore[override]
        self._hydrate()
        return super().__getitem__(key)

    def __contains__(self, key):  # type: ignore[override]
        self._hydrate()
        return super().__contains__(key)

    def __iter__(self):  # type: ignore[override]
        self._hydrate()
        return super().__iter__()

    def keys(self):  # type: ignore[override]
        self._hydrate()
        return super().keys()

    def values(self):  # type: ignore[override]
        self._hydrate()
        return super().values()

    def items(self):  # type: ignore[override]
        self._hydrate()
        return super().items()


ARCHETYPES: _ArchetypeRegistry = _ArchetypeRegistry()


__all__ = [
    "AgentContract", "AgentSpecError",
    "load_agents", "tools_schema_for_role", "validate_contracts",
    "ARCHETYPES",
]
