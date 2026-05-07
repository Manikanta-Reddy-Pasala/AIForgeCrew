"""Strict rules for the Verifier post-pass.

Each rule lives in its own module so adding / tightening / disabling
a single rule is a one-file change. The :data:`RULES` tuple here is
the orchestrator's canonical list — order matters only for output
readability, not correctness.
"""
from __future__ import annotations

from .too_many_subtickets import rule as too_many_subtickets
from .overscoped_subticket import rule as overscoped_subticket
from .missing_scope_allowlist import rule as missing_scope_allowlist
from .no_test_subticket import rule as no_test_subticket

RULES = (
    too_many_subtickets,
    overscoped_subticket,
    missing_scope_allowlist,
    no_test_subticket,
)

__all__ = [
    "RULES",
    "too_many_subtickets",
    "overscoped_subticket",
    "missing_scope_allowlist",
    "no_test_subticket",
]
