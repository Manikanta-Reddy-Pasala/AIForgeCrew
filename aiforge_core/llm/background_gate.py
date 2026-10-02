"""Background model calls wait for the user's turn instead of queueing in front of it.

The learner and memory roles run after the answer, many times a day. On a model
server with one slot, a call of theirs that starts while a chat run is in flight
holds the slot for seconds to minutes, and the user's next step waits behind it.
A background call now waits (polling) until no chat run is active, up to a bound,
and then goes. On a server with spare slots nothing waits.

Roles, not models: ``AIFORGE_BACKGROUND_ROLES`` (comma list, default
``learner,memory``). ``AIFORGE_BACKGROUND_YIELD_S`` is the longest wait (default
120 s; 0 turns it off).
"""
from __future__ import annotations

import os
import time

_POLL_S = 1.0


def background_roles() -> "frozenset[str]":
    raw = os.environ.get("AIFORGE_BACKGROUND_ROLES", "learner,memory")
    return frozenset(r.strip().lower() for r in raw.split(",") if r.strip())


def _limit_s() -> float:
    try:
        return float(os.environ.get("AIFORGE_BACKGROUND_YIELD_S", "120"))
    except ValueError:
        return 120.0


def wait_for_foreground(role: str) -> float:
    """Block while a chat run is in flight and ``role`` would share its slot.
    Returns the seconds waited. Never raises."""
    try:
        limit = _limit_s()
        if limit <= 0 or (role or "").lower() not in background_roles():
            return 0.0
        from aiforge_core.llm import slots
        from aiforge_core.runtime import chat_runs
        if slots.parallel_ok(role, slots.CHAT_ROLE):
            return 0.0
        t0 = time.monotonic()
        while chat_runs.any_active() and time.monotonic() - t0 < limit:
            time.sleep(_POLL_S)
        return time.monotonic() - t0
    except Exception:  # noqa: BLE001 — a gate never blocks a call for good
        return 0.0


__all__ = ["background_roles", "wait_for_foreground"]
