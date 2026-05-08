"""Per-archetype instruction strings for the v6 ADK pipeline.

Each archetype's prompt now lives in its own module so independent
edits don't collide. This package re-exports the original constants
(``PLANNER``, ``VERIFIER``, ``DOER``, ``FEEDBACK``, ``LEARNER``) plus
the new ``ARCHITECT`` structural-plan contract so existing imports
keep working unchanged.
"""
from __future__ import annotations

from .architect import PROMPT as ARCHITECT
from .planner import PROMPT as PLANNER
from .verifier import PROMPT as VERIFIER
from .doer import PROMPT as DOER
from .feedback import PROMPT as FEEDBACK
from .learner import PROMPT as LEARNER

__all__ = [
    "ARCHITECT", "PLANNER", "VERIFIER", "DOER", "FEEDBACK", "LEARNER",
]
