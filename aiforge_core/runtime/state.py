"""Pydantic-typed inter-node state — ADK 2.0 ``Event(state=...)`` shape
in plain-Pydantic so we don't carry the ADK 2.0 dep yet.

KISS: one ``WorkflowState`` base, one ``StateDelta`` for partial
updates, one ``Event`` envelope. Each agent role declares its own
state schema by subclassing ``WorkflowState``; orchestrator validates
inputs/outputs at the boundary.

When we cut over to ADK 2.0, swap our ``Event`` for ``google.adk.Event``
— field names match (``state``, ``message``, ``actions``).

Public surface:
- ``WorkflowState`` (Pydantic BaseModel base)
- ``StateDelta`` (Pydantic BaseModel for partial)
- ``Event(state=..., message=..., actions=...)``
"""
from __future__ import annotations

from typing import Any

try:
    from pydantic import BaseModel, Field, ConfigDict
except ImportError:  # pragma: no cover — pydantic is a hard dep elsewhere
    BaseModel = object  # type: ignore[assignment]
    Field = lambda *a, **kw: None  # type: ignore[assignment]
    ConfigDict = dict  # type: ignore[assignment]


class WorkflowState(BaseModel):
    """Base for typed cross-node state.

    Roles subclass: ``class PlannerState(WorkflowState): files: list[str]``.
    Extra keys allowed (forward-compat) but warned in dev.
    """
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    ticket: str | None = None
    role: str | None = None


class StateDelta(BaseModel):
    """Partial update shipped on an Event.

    Caller patches via ``state.model_copy(update=delta.model_dump())``.
    """
    model_config = ConfigDict(extra="allow")


class Event(BaseModel):
    """Envelope every agent yields — mirrors google.adk.Event shape."""
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    state: dict[str, Any] | None = None
    message: str | None = None
    actions: list[str] | None = None
    metadata: dict[str, Any] | None = None


def merge_state(
    base: WorkflowState | dict, delta: StateDelta | dict | None,
) -> dict:
    """Apply ``delta`` over ``base``. Returns a plain dict.

    Pydantic-friendly when both inputs are models; falls back to dict
    semantics otherwise.
    """
    if delta is None:
        if isinstance(base, BaseModel):
            return base.model_dump()
        return dict(base or {})
    base_d = base.model_dump() if isinstance(base, BaseModel) else dict(base or {})
    delta_d = (
        delta.model_dump(exclude_none=True)
        if isinstance(delta, BaseModel) else dict(delta or {})
    )
    merged = dict(base_d)
    merged.update(delta_d)
    return merged
