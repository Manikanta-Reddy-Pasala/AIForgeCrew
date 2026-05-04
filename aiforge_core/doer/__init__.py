"""Doer package — GA-only (custom code, no smolagents).

The historical name ``run_smolagents_doer`` is preserved so existing
callers don't break, but it now dispatches solely to the GA runner.
"""
from __future__ import annotations

from .orchestrator_bridge import run_smolagents_doer  # noqa: F401

__all__ = ["run_smolagents_doer"]
