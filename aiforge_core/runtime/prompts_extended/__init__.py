"""Prompts for the extended v6 archetypes (triage, researcher, refiner).

Each prompt now lives in its own module so independent edits don't
collide. This package re-exports the original TRIAGE / RESEARCHER /
REFINER constants for back-compat — existing imports like
``from aiforge_core.runtime.prompts_extended import TRIAGE`` keep
working unchanged.
"""
from __future__ import annotations

from .triage import PROMPT as TRIAGE
from .researcher import PROMPT as RESEARCHER
from .refiner import PROMPT as REFINER
from .ctx_memory import PROMPT as CTX_MEMORY
from .ctx_repomap import PROMPT as CTX_REPOMAP
from .ctx_conventions import PROMPT as CTX_CONVENTIONS

__all__ = [
    "TRIAGE", "RESEARCHER", "REFINER",
    "CTX_MEMORY", "CTX_REPOMAP", "CTX_CONVENTIONS",
]
