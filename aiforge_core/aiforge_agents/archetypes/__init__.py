"""Archetype implementations. Importing this package registers them all."""
from __future__ import annotations

# Side-effect imports — each module @register('s its class
from . import (  # noqa: F401
    architect,
    coordinator,
    doer,
    grounder,
    learner,
    planner,
    tester,
    understander,
    validator,
    verifier,
)
