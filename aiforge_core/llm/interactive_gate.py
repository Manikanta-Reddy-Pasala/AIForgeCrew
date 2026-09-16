"""Background model traffic steps aside while someone is being served.

Measured on a live box over a week: the memory learner made 936 of 989 model
calls (95%) and 174 of 194 model-minutes, in bursts of up to 436 calls lasting
50 minutes. 25 of the 53 interactive calls — chat, triage, enhancer — ran while
a learner call was in flight on the same endpoint, most of them for their whole
duration. On a model that serves one request at a time, that is a person
waiting behind memory folding that nobody is waiting for.

The rate limiter already separates a ``compaction`` category from ``chat``, but
only by requests-per-minute, and the compaction ceiling defaults to unbounded.
Nothing made background work YIELD. This does:

* every interactive send notes the time — in this process, and in a marker file
  so the API and the runner (separate processes) see each other;
* a background send waits while an interactive send happened in the last
  ``AIFORGE_BACKGROUND_YIELD_S`` seconds (default 45, roughly a slow chat call),
  up to the caller's budget, then goes ahead — memory is delayed, never starved.

It cannot pre-empt a background call already in flight; it stops the burst from
continuing to interleave. ``AIFORGE_BACKGROUND_YIELD_S=0`` disables it.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

_DEFAULT_WINDOW_S = 45.0
_MARKER_NAME = ".interactive-llm"
#: A marker refresh costs a syscall; a busy turn sends many calls a second.
_TOUCH_EVERY_S = 1.0
_POLL_S = 1.0

_LOCK = threading.Lock()
_last_local = 0.0
_last_touch = 0.0


def window_s() -> float:
    try:
        return max(0.0, float(os.environ.get("AIFORGE_BACKGROUND_YIELD_S", "")
                              or _DEFAULT_WINDOW_S))
    except ValueError:
        return _DEFAULT_WINDOW_S


def _marker() -> "str | None":
    try:
        from aiforge_core.config.paths import config_dir
        return os.path.join(str(config_dir()), _MARKER_NAME)
    except Exception:  # noqa: BLE001  # no config dir: in-process only
        return None


def note_interactive(now: "float | None" = None) -> None:
    """Record that an interactive send is going out. Never raises."""
    global _last_local, _last_touch
    if window_s() <= 0:
        return
    t = time.time() if now is None else now
    with _LOCK:
        _last_local = t
        if t - _last_touch < _TOUCH_EVERY_S:
            return
        _last_touch = t
    path = _marker()
    if not path:
        return
    try:
        Path(path).touch()
        os.utime(path, (t, t))
    except OSError:
        pass                     # a read-only config dir costs cross-process only


def busy_for(now: "float | None" = None) -> float:
    """Seconds until the interactive window closes (0 when nobody is served)."""
    win = window_s()
    if win <= 0:
        return 0.0
    t = time.time() if now is None else now
    last = _last_local
    path = _marker()
    if path:
        try:
            last = max(last, os.path.getmtime(path))
        except OSError:
            pass
    return max(0.0, last + win - t)


def yield_to_interactive(max_wait_s: float) -> float:
    """Block a BACKGROUND send while interactive work is being served.

    Returns the seconds waited. Bounded by ``max_wait_s``: past it the send
    goes ahead, so continuous interactive use delays memory work but can never
    stop it.
    """
    if max_wait_s <= 0 or window_s() <= 0:
        return 0.0
    waited = 0.0
    while waited < max_wait_s:
        left = busy_for()
        if left <= 0:
            break
        step = min(left, _POLL_S, max_wait_s - waited)
        time.sleep(step)
        waited += step
    return waited


def reset() -> None:
    """Forget the local timestamp and remove the marker (tests)."""
    global _last_local, _last_touch
    with _LOCK:
        _last_local = 0.0
        _last_touch = 0.0
    path = _marker()
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


__all__ = ["busy_for", "note_interactive", "reset", "window_s",
           "yield_to_interactive"]
