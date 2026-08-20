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
    return hour % 24                               # 24 = midnight


def open_now(now: "datetime | None" = None) -> bool:
    """May an OPPORTUNISTIC fold run right now?

    True when no daily hour is configured (the old always-on cadence), or when
    the local clock is at/after that hour. Before the hour the fold is skipped:
    the daily pass walks every session anyway, so nothing is lost — it just
    happens in the evening, which is what the operator asked for.
    """
    hour = at_hour()
    if hour is None:
        return True
    return (now or datetime.now()).hour >= hour


__all__ = ["at_hour", "open_now"]
