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


# Ceilings for ONE turn, applied where a turn's budget is COMPUTED: the
# settings validate each field in isolation, so a 1,000,000-step cap and 50
# extensions multiply into a 51-million-step, ~51-day turn that no UI copy
# warns about. Extensions are trimmed against these. They are NOT a floor
# under a deliberate 0 (= no cap): an uncapped turn is bounded by the deadline,
# the stall guards and Stop, not by these numbers.
_MAX_TURN_STEPS = 1_000_000
_MAX_TURN_SECONDS = 24 * 3600

# What a TYPO falls back to. Deliberately not "the default": the defaults are
# now 0 (= no guard), so treating a negative as "use the default" would turn
# the typo the sign check exists to catch into the very thing it guards
# against. A malformed value gets a real, finite budget instead.
_TYPO_STEPS = 2000
_TYPO_SECONDS = 3600.0


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
    """Runaway step cap for one chat turn (``AIFORGE_CHAT_SAFETY_CAP``).

    ``0`` = NO cap: the turn ends when the agent is done, when a stall guard
    fires, when the wall-clock deadline hits, or when the user presses Stop.
    Same convention as the deadline, so "no limits" is one obvious value in
    both fields rather than a big number in one and a zero in the other.

    A NEGATIVE value is a typo, not a request: clamping it toward 0 would let
    ``AIFORGE_CHAT_SAFETY_CAP=-1`` (or an ``$((N-1))`` underflow in a unit
    file) silently remove the runaway guard on every box in the fleet. Only a
    literal 0 turns the cap off; anything below that warns and falls back to
    the default — the same rule ``_turn_deadline_s`` already applies."""
    raw = _explicit_float("chat_safety_cap", "AIFORGE_CHAT_SAFETY_CAP")
    if raw is not None and raw < 0:
        log.warning("chat_safety_cap=%r is negative — using %d steps. "
                    "Use 0 for no cap.", raw, _TYPO_STEPS)
        return _TYPO_STEPS
    return _setting("chat_safety_cap", "AIFORGE_CHAT_SAFETY_CAP", 0,
                    lo=0, hi=1_000_000)


def _explicit_float(name: str, env: str) -> "float | None":
    """The operator's EXPLICIT value (store, else env) as a FLOAT, before any
    clamp — so a caller can tell "they typed something invalid" from "they
    typed nothing".

    Float, not int: ``int(float(x))`` truncates TOWARD ZERO, so ``-0.5`` would
    arrive at a ``< 0`` test as ``0`` — silently disabling the guard on exactly
    the underflow the sign check exists to catch. ``_turn_deadline_s`` compares
    before truncating for the same reason."""
    try:
        from aiforge_core.config import runtime_settings as _rs
        val = _rs.stored(name)
        if val is not None:
            return float(val)
    except Exception:  # noqa: BLE001 — settings must never break a turn
        pass
    raw = os.environ.get(env)
    if raw in (None, ""):
        return None
    try:
        return float(raw)        # "0.0" in a unit file is an ordinary thing
    except (TypeError, ValueError):
        return None


def _turn_deadline_s() -> float:
    """Wall-clock backstop for one chat turn, seconds; 0 disables
    (``AIFORGE_CHAT_TURN_DEADLINE_S``, DEFAULT 0 = no deadline).

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
                            "using %ss", raw, _TYPO_SECONDS)
                stored = _TYPO_SECONDS
    val = float(0 if stored is None else stored)
    if val < 0:
        # 0 means "no deadline", so clamping a negative toward 0 would DISABLE
        # the runaway guard on a typo. Clamp toward safety instead.
        log.warning("chat turn deadline %r is negative — using %ss instead",
                    val, _TYPO_SECONDS)
        val = _TYPO_SECONDS
    return min(float(_MAX_TURN_SECONDS), val)


def _extension_budget(cap_base: int, turn_budget_s: float) -> int:
    """Extensions this turn may spend, trimmed so the PRODUCT of cap ×
    (1 + extensions) — in steps and in wall clock — stays inside the absolute
    per-turn ceilings.

    An UNCAPPED step guard (0) simply skips the STEP clamp — the wall-clock
    extensions are untouched. Gating both axes on the step cap made "no step
    cap" stop a turn SOONER than a 2000-step one (its deadline could no longer
    extend), which is the opposite of what the operator asked for. The 24h
    ceiling that worried me is already enforced by the _MAX_TURN_SECONDS trim
    below."""
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


def _unattended_cap() -> int:
    """Step cap for a run with NOBODY watching (``session_id is None``).

    The chat loop's only interactive brake is the Stop button, and its cancel
    check is gated on a session id — so the jobs scheduler, the analysis
    fan-out, the subtask runners and text_doer have no way to be stopped at
    all. "No limits" is a promise made to a user sitting in front of a chat;
    handing it to a cron-fired daemon thread means it burns tokens until the
    process dies. Those runs keep a cap: Settings → "Background step cap", or
    ``AIFORGE_CHAT_UNATTENDED_CAP``.

    NOTE the deliberate difference from :func:`_safety_cap`: 0 here is NOT "no
    cap". This value exists precisely because nothing else stops these runs, so
    0 (and any negative) is treated as a typo — warn, use the default — rather
    than as a request to remove the only brake a background job has. Clamping
    it to 1 instead, as the first cut did, turned every scheduled job into a
    one-step turn."""
    raw = _explicit_float("chat_unattended_cap", "AIFORGE_CHAT_UNATTENDED_CAP")
    if raw is not None and raw < 1:
        log.warning("chat_unattended_cap=%r — a background run has no Stop "
                    "button, so it cannot be uncapped. Using 2000.", raw)
        return 2000
    return _setting("chat_unattended_cap", "AIFORGE_CHAT_UNATTENDED_CAP", 2000,
                    lo=1, hi=_MAX_TURN_STEPS)


def _cap_extensions() -> int:
    """How many times a PROGRESSING turn may extend the step cap / deadline
    before it is stopped for real (``AIFORGE_CHAT_CAP_EXTENSIONS``, default 2).
    0 = never extend (the old hard stop)."""
    return _setting("chat_cap_extensions", "AIFORGE_CHAT_CAP_EXTENSIONS", 2,
                    lo=0, hi=50)


__all__ = ["_safety_cap", "_turn_deadline_s", "_cap_extensions",
           "_extension_budget", "_unattended_cap",
           "_MAX_TURN_STEPS", "_MAX_TURN_SECONDS"]
