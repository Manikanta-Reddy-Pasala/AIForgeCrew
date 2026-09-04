"""Workflow specifications + registry + dispatcher.

A :class:`WorkflowSpec` declares:

* ``id`` — stable string used in the DB (``tickets.route_workflow``).
* ``label`` — human-readable name shown in the UI dropdown.
* ``triggers`` — auto-detection rules consumed by
  :func:`aiforge_core.workflows.detector.detect_route`.
* ``handler`` — fully-qualified ``module:func`` to dispatch the ticket
  through. Function signature: ``handler(ticket: dict, log=None) -> dict``
  (returns the doer-outcome-shaped dict the orchestrator expects).
* ``required_attachments`` / ``optional_inputs`` — used by the UI to
  prompt for missing data before submit.

Registry is process-global. Tests can mutate freely; the API copies
values out before returning so external callers can't poison it.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable


@dataclass
class WorkflowSpec:
    id: str
    label: str
    description: str = ""
    handler: str = ""
    triggers: dict[str, Any] = field(default_factory=dict)
    required_attachments: list[str] = field(default_factory=list)
    optional_inputs: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_public_dict(self) -> dict[str, Any]:
        """UI-safe view — no handler import paths, no internals.

        DEEP copy of ``triggers``. ``dict(self.triggers)`` copied the mapping
        and shared every list inside it, so a caller appending to the returned
        ``triggers["keywords_any"]`` edited the process-global registry — the
        exact poisoning this module's docstring says cannot happen.
        """
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "triggers": deepcopy(self.triggers),
            "required_attachments": list(self.required_attachments),
            "optional_inputs": list(self.optional_inputs),
            "tags": list(self.tags),
        }


REGISTRY: dict[str, WorkflowSpec] = {}


def register(spec: WorkflowSpec) -> WorkflowSpec:
    """Add a spec to the registry. Idempotent on id (re-register replaces)."""
    if not spec.id:
        raise ValueError("WorkflowSpec.id is required")
    if not spec.handler:
        raise ValueError(f"WorkflowSpec({spec.id!r}).handler is required")
    REGISTRY[spec.id] = spec
    return spec


def get(workflow_id: str) -> WorkflowSpec | None:
    return REGISTRY.get(workflow_id)


def list_all() -> list[WorkflowSpec]:
    return sorted(REGISTRY.values(), key=lambda w: w.id)


def _resolve_handler(handler_path: str) -> Callable[..., dict]:
    """Import ``module:func`` lazily. Cached implicitly by importlib."""
    if ":" not in handler_path:
        raise ValueError(
            f"handler {handler_path!r} must be 'module:function'"
        )
    mod_path, func_name = handler_path.split(":", 1)
    mod = import_module(mod_path)
    fn = getattr(mod, func_name, None)
    if fn is None:
        raise ImportError(
            f"handler {handler_path!r} — function {func_name} not found "
            f"in module {mod_path}"
        )
    if not callable(fn):
        raise TypeError(f"handler {handler_path!r} is not callable")
    return fn


def dispatch(workflow_id: str, ticket: dict, *, log=None,
             **kwargs) -> dict:
    """Look up + invoke the workflow handler for ``workflow_id``.

    Returns a doer-outcome-shaped dict. Raises ``KeyError`` when the id
    is unknown — callers should pre-check via ``get(workflow_id)`` if
    they want to surface UI-friendly errors.
    """
    spec = REGISTRY.get(workflow_id)
    if spec is None:
        raise KeyError(f"unknown workflow id: {workflow_id!r}")
    fn = _resolve_handler(spec.handler)
    return fn(ticket, log=log, **kwargs)
