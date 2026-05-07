"""Triage-driven model routing for Doer / Researcher / Refiner / Triage.

The package is split so each concern is one file:

    tiers.py        — per-role tier lists + ``for_role(role)`` lookup
    routing.py      — complexity → tier ``pick(role, complexity)``
    escalation.py   — ``next_doer_model_after_fail(current)``

Public re-exports here preserve the old monolithic
``aiforge_core.runtime.model_router.pick(...)`` import path; nothing
in tests or runtime code needs to change.
"""
from __future__ import annotations

from .routing import RoutingDecision, pick
from .escalation import next_doer_model_after_fail
from .tiers import (
    DOER as DOER_TIERS,
    RESEARCHER as RESEARCHER_TIERS,
    REFINER as REFINER_TIERS,
    TRIAGE as TRIAGE_TIERS,
)

__all__ = [
    "RoutingDecision", "pick", "next_doer_model_after_fail",
    "DOER_TIERS", "RESEARCHER_TIERS", "REFINER_TIERS", "TRIAGE_TIERS",
]
