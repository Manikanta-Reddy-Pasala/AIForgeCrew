"""Named workflow registry — alternative to the generic 9-stage code cascade.

A *workflow* is a deterministic, domain-specific pipeline (e.g. Tally
trial-balance reconciliation) that runs in place of the LLM cascade
when the ticket clearly maps to one. The registry is the single source
of truth for which workflows exist, how to detect them from a ticket,
and how to dispatch.

Usage:

    from aiforge_core.workflows import REGISTRY, detect_route, dispatch

    route = detect_route(intent, body, attachments)
    if route.kind == "workflow":
        outcome = dispatch(route.workflow_id, ticket, log=log)
    else:
        outcome = run_code_cascade(ticket)

Registering a workflow:

    @register
    class MyWorkflow(WorkflowSpec):
        id = "my-workflow"
        ...

Registries auto-import bundled workflows on package load (see
``aiforge_core.workflows._builtin``).
"""
from .registry import (
    WorkflowSpec,
    REGISTRY,
    register,
    get,
    list_all,
    dispatch,
)
from .detector import (
    TicketRoute,
    detect_route,
)

# Trigger built-in workflow registration. Importing this side-effect
# module is what populates REGISTRY with the bundled handlers.
from . import _builtin  # noqa: F401

__all__ = [
    "WorkflowSpec",
    "REGISTRY",
    "register",
    "get",
    "list_all",
    "dispatch",
    "TicketRoute",
    "detect_route",
]
