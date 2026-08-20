"""Per-turn budget limits for the chat loop — step cap, wall-clock deadline,
and how many times a turn that is STILL MAKING PROGRESS may extend them.

Each knob resolves the same way as every other operator-tunable value:
``runtime_settings.json`` (the Settings UI writes here) → the documented env
var → the built-in default. So an operator can raise the cap from the UI
without editing a unit file, and a headless box keeps its env override.

Why extensions exist: the step cap and the turn deadline are RUNAWAY guards,
not task budgets. A long, legitimate refactor hitting 2000 steps used to be
killed with "(stopped: … raise AIFORGE_CHAT_SAFETY_CAP if this was real work)"
— the work was thrown away and the user had to re-run the whole turn with a
bigger number. Instead the loop now asks: is this turn still producing NEW
work? If yes, condense the history and grant another budget; if it is only
spinning, stop as before.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.chat_limits")


# Absolute ceilings for ONE turn, whatever the operator typed. The settings
# validate each field in isolation, so a 1,000,000-step cap and 50 extensions
# multiply into a 51-million-step, ~51-day turn that no UI copy warns about.
# Extensions are trimmed against these instead.
_MAX_TURN_STEPS = 1_000_000
_MAX_TURN_SECONDS = 24 * 3600


def _setting(name: str, env: str, default: int, *, lo: int, hi: int) -> int:
    """Stored setting → env var → default, clamped into [lo, hi].

    ``runtime_settings`` already resolves store-then-env and rejects an
    out-of-bounds STORED value; the clamp here also covers a bad ENV value
    (which the store never validates) so a typo can't wedge a turn."""
    try:
        from aiforge_core.config import runtime_settings as _rs
        val = int(_rs.get(name))
    except Exception:  # noqa: BLE001 — settings must never break a turn
        raw = os.environ.get(env)
        try:
            val = int(raw) if raw else default
        except (TypeError, ValueError):
            val = default
    return max(lo, min(hi, val))


def _safety_cap() -> int:
    """Runaway step cap for one chat turn (``AIFORGE_CHAT_SAFETY_CAP``)."""
    return _setting("chat_safety_cap", "AIFORGE_CHAT_SAFETY_CAP", 2000,
                    lo=1, hi=1_000_000)


def _turn_deadline_s() -> float:
    """Wall-clock backstop for one chat turn, seconds; 0 disables
    (``AIFORGE_CHAT_TURN_DEADLINE_S``, default 3600).

    FRACTIONAL env values are honoured: the settings store is integer-only, so
    the env var is parsed here rather than through it — ``7200.5`` is a
    perfectly ordinary thing to have in a unit file, and a sub-second value is
    what a test harness needs. A stored (UI) value still wins, and the store
    reads a float env var as its integer part so the card cannot display a
    number the runtime is not using.
    """
    try:
        from aiforge_core.config import runtime_settings as _rs
        stored = _rs.stored("chat_turn_deadline_s")
    except Exception:  # noqa: BLE001 — settings must never break a turn
        stored = None
    if stored is None:
        raw = os.environ.get("AIFORGE_CHAT_TURN_DEADLINE_S")
        if raw:
            try:
                stored = float(raw)
            except (TypeError, ValueError):
                log.warning("AIFORGE_CHAT_TURN_DEADLINE_S=%r is not a number — "
                            "using the 3600s default", raw)
    val = float(3600 if stored is None else stored)
    if val < 0:
        # 0 means "no deadline", so clamping a negative toward 0 would DISABLE
        # the runaway guard on a typo. Clamp toward safety instead.
        log.warning("chat turn deadline %r is negative — using the 3600s "
                    "default", val)
        val = 3600.0
    return min(float(_MAX_TURN_SECONDS), val)


def _extension_budget(cap_base: int, turn_budget_s: float) -> int:
    """Extensions this turn may spend, trimmed so the PRODUCT of cap ×
    (1 + extensions) — in steps and in wall clock — stays inside the absolute
    per-turn ceilings."""
    ext = _cap_extensions()
    if ext <= 0:
        return 0
    if cap_base > 0:
        ext = min(ext, max(0, (_MAX_TURN_STEPS // cap_base) - 1))
    if turn_budget_s > 0:
        ext = min(ext, max(0, int(_MAX_TURN_SECONDS // turn_budget_s) - 1))
    # With the deadline disabled (0) there is no wall clock to bound — the
    # operator removed that guard deliberately; the step ceiling still applies.
    return ext


def _cap_extensions() -> int:
    """How many times a PROGRESSING turn may extend the step cap / deadline
    before it is stopped for real (``AIFORGE_CHAT_CAP_EXTENSIONS``, default 2).
    0 = never extend (the old hard stop)."""
    return _setting("chat_cap_extensions", "AIFORGE_CHAT_CAP_EXTENSIONS", 2,
                    lo=0, hi=50)


__all__ = ["_safety_cap", "_turn_deadline_s", "_cap_extensions",
           "_extension_budget", "_MAX_TURN_STEPS", "_MAX_TURN_SECONDS"]
