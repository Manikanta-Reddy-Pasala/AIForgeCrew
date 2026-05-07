"""Strict structural rules layered on top of the LLM Verifier verdict.

This package re-exports the original public surface so existing imports
(``verifier_strict.apply``, ``verifier_strict.RULES``, individual rules,
``MAX_SUBTICKETS`` etc.) keep working unchanged.

Per-rule modules live under :mod:`.rules`; tests can import a single
rule without pulling the rest of the package.
"""
from __future__ import annotations

from .apply import apply
from .rules import (
    RULES,
    too_many_subtickets as rule_too_many_subtickets,
    overscoped_subticket as rule_overscoped_subticket,
    missing_scope_allowlist as rule_missing_scope_allowlist,
    no_test_subticket as rule_no_test_subticket,
)
from .rules.too_many_subtickets import MAX_SUBTICKETS
from .rules.overscoped_subticket import MAX_FILES_PER_SUBTICKET

# ``RULES`` historically was a module attribute test code monkey-patched
# (test_verifier_strict.py replaces it with a single buggy rule). We
# keep that override path working by re-exposing it as a settable name.
__all__ = [
    "apply", "RULES",
    "MAX_SUBTICKETS", "MAX_FILES_PER_SUBTICKET",
    "rule_too_many_subtickets", "rule_overscoped_subticket",
    "rule_missing_scope_allowlist", "rule_no_test_subticket",
]
