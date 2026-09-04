"""When may memory compaction run?

Compaction is LLM-heavy. The operator pins it to ONE evening hour
(``AIFORGE_COMPACT_AT_HOUR``, default 18 local) precisely so it does not chew
the machine — and the user's attention — during the working day. That hour is
honoured by the daily scheduled pass; this module lets the OPPORTUNISTIC folds
(the one fired when you switch chats) honour it too, instead of running a fold
at 09:00 the moment a new chat is opened.

``at_hour()`` is the parsed hour or None (off / hourly schedule). ``open_now()``
answers the only question a caller has: may I fold right now?
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

log = logging.getLogger("aiforge.compact_window")


def disabled() -> bool:
    """True when memory compaction's LLM folds are turned OFF.

    THE SINGLE SOURCE OF TRUTH for the on/off switch. Every compaction path —
    the daily scheduled pass (api._start_daily_reindex), the boot fold
    (migrations._startup_compact), the sync-loop OKF fold (okf.tiers) and the
    brief consolidation (work_notes.consolidate) — reads it here, so the default
    can never drift between them (it used to: the flag gated the scheduler and
    the boot fold but NOT the sync-loop fold, which kept firing regardless).

    ENABLED BY DEFAULT: an unset flag reads as ON. Compaction is safe to leave
    on because the per-category rate limiter caps it at ``compaction_rpm``
    (default 5/min) — it can no longer flood the provider or the working day.
    Set ``AIFORGE_COMPACT_DISABLE=1`` to skip the LLM fold entirely (the cheap
    structural folds — file moves, capture sweeps — still run).
    """
    return os.environ.get("AIFORGE_COMPACT_DISABLE", "0").strip().lower() in (
        "1", "true", "yes")


def at_hour() -> "int | None":
    """Local hour of the single daily compaction pass, or None when the daily
    schedule is off (explicit ``off``/``0``, or an explicit hourly interval)."""
    raw = os.environ.get("AIFORGE_COMPACT_AT_HOUR")
    if raw is None:
        try:
            if int(os.environ.get("AIFORGE_COMPACT_EVERY_H", "")) > 0:
                return None
        except (TypeError, ValueError):
            pass
    raw = (raw if raw is not None else "18").strip().lower()
    if raw in ("", "off", "none", "false", "no"):
        return None
    try:
        hour = int(raw)
    except ValueError:
        log.warning("AIFORGE_COMPACT_AT_HOUR=%r is not an hour — using 18", raw)
        return 18
    if hour <= 0:
        # 0 is OFF, not midnight (every sibling knob reads =0 that way).
        return None
    if hour > 24:
        log.warning("AIFORGE_COMPACT_AT_HOUR=%r out of range — using 23", raw)
        return 23
    # midnight is written as 24 by operators who mean "end of day"
    return hour % 24


def daily_pass_registered() -> bool:
    """Will the scheduled evening pass actually run on this install?

    Deferring an opportunistic fold to "the daily pass" is only safe if that
    pass exists. It does NOT when the periodic engine is off, when jobs are
    disabled, or when AIFORGE_REINDEX_DAILY=0 short-circuits the startup
    handler that registers it (``api._start_daily_reindex``) — in those
    configurations the on-switch fold is the ONLY compaction there is, and
    gating it would mean nothing ever folds.
    """
    if os.environ.get("AIFORGE_PERIODIC_DISABLE", "") in ("1", "true", "yes"):
        return False
    if os.environ.get("AIFORGE_JOBS_DISABLE", "") in ("1", "true", "yes"):
        return False
    if os.environ.get("AIFORGE_REINDEX_DAILY", "1") in ("0", "false", "no"):
        return False
    return at_hour() is not None


def catch_up_enabled() -> bool:
    """Operator opted back into "run it whenever you next wake" (the pre-window
    behaviour). Opens the opportunistic gate too — the same escape hatch has to
    move both halves, or setting it fixes the scheduler and leaves the fold
    dead 18 hours a day."""
    return os.environ.get("AIFORGE_COMPACT_CATCH_UP", "0") in ("1", "true", "yes")


def open_now(now: "datetime | None" = None) -> bool:
    """May an OPPORTUNISTIC fold run right now?

    True when no daily hour is configured (the old always-on cadence), when the
    daily pass would not run at all on this install, when the operator asked
    for catch-up, or when the local clock is at/after the hour. Otherwise the
    fold is skipped: the daily pass walks every session anyway, so nothing is
    lost — it just happens in the evening, which is what the operator asked for.
    """
    hour = at_hour()
    if hour is None or not daily_pass_registered() or catch_up_enabled():
        return True
    return (now or datetime.now()).hour >= hour


__all__ = ["at_hour", "open_now", "daily_pass_registered", "catch_up_enabled"]
