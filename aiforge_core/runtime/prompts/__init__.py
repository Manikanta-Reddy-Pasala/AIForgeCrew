"""Per-archetype instruction strings for the v6 ADK pipeline.

Each archetype's prompt now lives in its own module so independent
edits don't collide. This package re-exports the original constants
(``PLANNER``, ``VERIFIER``, ``DOER``, ``FEEDBACK``, ``LEARNER``) so
existing imports keep working unchanged.
"""
from __future__ import annotations

from .planner import PROMPT as PLANNER
from .verifier import PROMPT as VERIFIER
from .doer import PROMPT as DOER
from .feedback import PROMPT as FEEDBACK
from .learner import PROMPT as LEARNER

__all__ = ["PLANNER", "VERIFIER", "DOER", "FEEDBACK", "LEARNER"]
