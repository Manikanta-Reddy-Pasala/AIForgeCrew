"""Holding back after a 429: recording it, and how long the hold lasts."""
from __future__ import annotations

import time

from ._rate_settings import (
    _ANY,
    _WINDOW_LOCK,
    _hold_cap,
    _holds,
    _now,
    _sends,
)


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.llm.rate_limiter as package
    return package


def _trim_locked(now: float) -> None:
    """Drop sends that have aged out of the 60s window. Caller holds
    ``_WINDOW_LOCK``."""
    cut = now - 60.0
    i = 0
    while i < len(_sends) and _sends[i][0] <= cut:
        i += 1
    if i:
        del _sends[:i]


def note_rate_limited(retry_after_s: float = 0.0,
                      provider: "str | None" = None) -> None:
    """The SERVER said we are over ITS limit. Hold every caller of THAT
    provider ON THIS MACHINE until its window can plausibly have cleared.

    Without this, one rejection teaches nobody: our own count sits comfortably
    under our ceiling (the ceiling is only ever an ESTIMATE of the server's
    rule — the server counts a window we never observe, and other tools on
    other machines may share the same account), so the next caller sends into
    the same wall, and the model chain spends another request discovering it.

    Written to the shared store first: across processes this is the half that
    matters most, because only ONE of them gets the 429 and the others were
    sending into a wall the server had already named.

    A SEPARATE HOLD, not a synthetic fill of ``_sends``. Filling the window
    silently did nothing in the one situation that matters most: when callers
    have been overrunning the ceiling, ``len(_sends)`` is already at or above
    capacity, so "top the window up to capacity" is ``range(0)`` — a no-op at
    exactly the moment a rejection is most likely to arrive.

    APPLIES EVEN AT ``llm_max_rpm=0``. Zero means "I have not asked you to
    throttle me", which is a statement about our own preference; it is not
    permission to ignore a provider that has just refused us. Obeying a
    rejection is never the wrong thing to do, and the hold is bounded by the
    caller's ``max_wait_s`` like every other wait here.

    BOUNDED by :func:`_hold_cap`, because ``retry_after_s`` is a number a
    remote server chose.
    """
    hold = retry_after_s if retry_after_s and retry_after_s > 0 else 60.0
    hold = min(hold, _hold_cap())
    key = provider or _ANY
    # SHARED first. Across processes this is the half that matters most: only
    # ONE of them gets the 429, and without a shared hold the others keep
    # sending into a wall the server has already named. Wall clock here, not
    # monotonic, because that is the only clock two processes agree on.
    sw = _pkg()._shared()
    if sw is not None:
        sw.set_hold(key, time.time() + hold, cap=_hold_cap())
    with _WINDOW_LOCK:
        # The in-process copy is kept regardless: it is the fallback when the
        # shared store is unavailable, and it costs nothing to maintain.
        _holds[key] = max(_holds.get(key, 0.0), _now() + hold)


def _hold_left_locked(now: float, provider: "str | None") -> float:
    """Seconds left on a hold that applies to ``provider``. Caller holds
    ``_WINDOW_LOCK``."""
    left = 0.0
    for key in ({_ANY, provider or _ANY}):
        until = _holds.get(key)
        if until is not None:
            left = max(left, until - now)
    return max(0.0, left)


def held_for(provider: "str | None" = None) -> float:
    """Seconds still left on a server-imposed hold; 0 when none.

    The longer of what THIS process knows and what the machine knows — a hold
    another process earned is just as binding as one we earned ourselves.
    """
    with _WINDOW_LOCK:
        mine = _hold_left_locked(_now(), provider)
    sw = _pkg()._shared()
    if sw is None:
        return mine
    shared = sw.hold_left((_ANY, provider or _ANY), cap=_hold_cap())
    return mine if shared is None else max(mine, shared)


def window_scope() -> str:
    """"machine" when the cross-process window is live, else "process".

    An operator asking "why am I still rate limited with the setting applied"
    cannot answer it without this: a silent fallback puts the ceiling back to
    per-process, which is the very bug the shared window exists to fix.
    """
    sw = _pkg()._shared()
    # writable(), not count(): a read never blocks on a writer in WAL, so
    # count() reports a healthy number while every take() is failing — the one
    # state this exists to reveal.
    return "machine" if (sw is not None and sw.writable()) else "process"
