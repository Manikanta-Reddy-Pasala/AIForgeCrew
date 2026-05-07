"""Escalate the Doer to a stronger model after a compile-fail.

Option F in the upgrade list (see ``docs/superpowers/specs/...``):
escalate on the FIRST compile-fail instead of waiting for two
consecutive failures and halting per the YAML termination contract.
"""
from __future__ import annotations

from . import tiers


def next_doer_model_after_fail(current: str) -> str | None:
    """Return the next stronger Doer model after ``current`` failed.

    Returns:
      The model id one tier above ``current`` in :data:`tiers.DOER`,
      or ``None`` once the top tier has been reached. ``current`` not
      being a known tier (operator override etc.) jumps straight to the
      top — better to pay for one cloud turn than spin in a loop.
    """
    try:
        idx = tiers.DOER.index(current)
    except ValueError:
        return tiers.DOER[-1]
    if idx >= len(tiers.DOER) - 1:
        return None
    return tiers.DOER[idx + 1]


__all__ = ["next_doer_model_after_fail"]
