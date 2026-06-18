"""Per-archetype instruction strings for the v6 ADK pipeline.

Each archetype's prompt now lives in its own module so independent
edits don't collide. This package re-exports the original constants
(``PLANNER``, ``VERIFIER``, ``DOER``, ``FEEDBACK``, ``LEARNER``) plus
the new ``ARCHITECT`` structural-plan contract so existing imports
keep working unchanged.
"""
from __future__ import annotations

from .architect import PROMPT as ARCHITECT
from .enhancer import ENHANCER
from .planner import PROMPT as PLANNER
from .verifier import PROMPT as VERIFIER
from .doer import PROMPT as DOER
from .feedback import PROMPT as FEEDBACK
from .learner import PROMPT as LEARNER
from .validator import VALIDATOR
from .live_verifier import LIVE_VERIFIER
from .verify_correctness import PROMPT as VERIFY_CORRECTNESS
from .verify_scope import PROMPT as VERIFY_SCOPE
from .verify_risk import PROMPT as VERIFY_RISK
from .gap_eval import PROMPT as GAP_EVAL

__all__ = [
    "ARCHITECT", "ENHANCER", "PLANNER", "VERIFIER", "DOER",
    "FEEDBACK", "LEARNER", "VALIDATOR", "LIVE_VERIFIER",
    "VERIFY_CORRECTNESS", "VERIFY_SCOPE", "VERIFY_RISK", "GAP_EVAL",
]
