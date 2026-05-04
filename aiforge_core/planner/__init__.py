"""Planner package — GA-only (custom code, no smolagents).

`run_planner` re-exports the GA runner so existing call sites
(``from aiforge_core.planner import run_planner``) keep working.
"""
from __future__ import annotations

from .ga_runner import run_planner_via_ga as run_planner

__all__ = ["run_planner"]
