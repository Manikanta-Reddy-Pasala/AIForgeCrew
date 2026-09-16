"""md_store internals: which model role does memory work.

Distilling a transcript into facts and consolidating a brief are JUDGEMENT
tasks — what is durable, what is already known, what contradicts what — so they
run on a reasoning ("thinking") role, not the fast direct-output one that
titles and classifies. Both role names are config, never a model id, so this
stays portable across boxes (see the registry for model assignment).

`model_registry` warns that a reasoning model can return EMPTY on a short
direct-output task; that is exactly why `fallback_role()` exists and why the
extract path retries on it once instead of dropping the window.
"""
from __future__ import annotations

import os

#: Role that owns memory distillation/consolidation. Registered in agents.yaml.
_DEFAULT_ROLE = "memory"
#: Fast role to retry on when the thinking model answers with nothing.
_FALLBACK_ROLE = "learner"


def memory_role() -> str:
    """The role memory work runs on (``AIFORGE_MEMORY_MODEL_ROLE`` to override,
    e.g. back to ``learner`` on a box with no reasoning model loaded)."""
    return (os.environ.get("AIFORGE_MEMORY_MODEL_ROLE", "") or "").strip() \
        or _DEFAULT_ROLE


def fallback_role() -> str:
    """Fast role used for ONE retry when the thinking role returns nothing."""
    return (os.environ.get("AIFORGE_MEMORY_FALLBACK_ROLE", "") or "").strip() \
        or _FALLBACK_ROLE


def is_thinking_role(role: str) -> bool:
    """True when ``role`` is the reasoning one — callers widen the token budget
    (a thinking model spends part of it before the first output token)."""
    return (role or "").strip().lower() == memory_role().lower()


__all__ = ["fallback_role", "is_thinking_role", "memory_role"]
