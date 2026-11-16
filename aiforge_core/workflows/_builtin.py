"""Built-in workflow registrations.

Importing this module is the side-effect that populates :data:`REGISTRY`.
Add new generic workflows by appending more :func:`register` calls.

The product ships no built-in workflow today. Project-specific workflows
live in the per-host config folder (``$AIFORGE_CONFIG_DIR/workflows/*.py``)
and are loaded by :mod:`aiforge_core.workflows._user`.
"""
from __future__ import annotations

from .registry import WorkflowSpec, register  # noqa: F401 — the API to extend
