"""Which ceiling a send counts against right now: chat is active or idle,
and how much of the global window idle compaction may take."""
from __future__ import annotations

import math
import time

from ._rate_holds import _trim_locked
from ._rate_settings import _WINDOW_LOCK, _now, _sends


def _pkg():
    """``rate_limiter``, looked up on each call so a patch there is used."""
    import aiforge_core.llm.rate_limiter as package
    return package


def _chat_sends_recent() -> "int | None":
    """Chat-category sends in the current minute, or None if the shared
    window is on but could not be read.

    None is not idle. The shared window is where chat sends live, and a
    failed read must not raise compaction to the global ceiling while chat
    is using that window. The in-process list is only the fallback when the
    shared window is off.
    """
    sw = _pkg()._shared()
    if sw is not None:
        try:
            n = sw.count(cat="chat")
        except Exception:  # noqa: BLE001
            n = None
        if n is None:
            return None
        return int(n)
    with _WINDOW_LOCK:
        _trim_locked(_now())
        return sum(1 for _, c in _sends if c == "chat")


#: Share of the global ceiling that idle compaction leaves free, so the next
#: chat send never finds the whole minute already spent on folding.
_CHAT_HEADROOM_FRAC = 0.25
#: How long after an interactive send chat still counts as active.
_CHAT_ACTIVE_S = 60.0


def _chat_active() -> bool:
    """True when a person was served in the last minute.

    The window alone cannot say: an uncapped chat send (the default) is never
    written to it. The interactive gate stamps every chat send, capped or not.
    An unreadable shared window counts as active.
    """
    try:
        from . import interactive_gate as _gate
        if _gate.since_last() < _CHAT_ACTIVE_S:
            return True
    except Exception:  # noqa: BLE001
        pass
    return _chat_sends_recent() != 0


def _category_limit(cat: str) -> float:
    """Per-category rpm for this send, evaluated at the moment of the send.

    Compaction's stored cap (default 5) applies while chat is active, so a
    fold cannot crowd out the person. On an idle box it rises: to the global
    ceiling minus a reserve kept for the next chat send, or — with no global
    ceiling — to unbounded. A stored 0 stays "no category cap".
    """
    base = _pkg()._cat_rpm(cat)
    if cat != "compaction" or base <= 0 or _chat_active():
        return base
    g = _pkg().global_rpm()
    if g <= 0:
        return 0.0
    headroom = max(1.0, float(math.ceil(g * _CHAT_HEADROOM_FRAC)))
    return max(base, g - headroom)


def _cancelled(cancel) -> bool:
    try:
        return cancel is not None and bool(cancel.is_set())
    except Exception:  # noqa: BLE001
        return False


def _sleep_cancellable(seconds: float, cancel) -> float:
    """Sleep up to ``seconds``, waking every quarter second to check
    ``cancel``. Returns the seconds actually slept, so Stop ends a parked
    call within that quarter second rather than after a 5s step."""
    if cancel is None:
        time.sleep(seconds)
        return seconds
    slept = 0.0
    while slept < seconds and not _cancelled(cancel):
        step = min(0.25, seconds - slept)
        time.sleep(step)
        slept += step
    return slept
