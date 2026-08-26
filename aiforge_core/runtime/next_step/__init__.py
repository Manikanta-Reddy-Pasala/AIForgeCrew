"""Predicting the next step, and deciding whether to take it."""
from __future__ import annotations

from aiforge_core.runtime.next_step import _risk

ACT, OFFER = _risk.ACT, _risk.OFFER

__all__ = ["ACT", "OFFER"]
