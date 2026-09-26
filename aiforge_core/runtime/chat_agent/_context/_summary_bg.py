"""The model summary of a condensed slice, written behind the turn.

Keyed by the RUN, never the session. ``session_id`` is None for every
unattended run (the text Doer, the analysis fan-out), and those run in
parallel — a session key let one run's summary land in another run's prompt.
A run without a key gets no model summary; the heuristic breadcrumb stands.

Each summary carries the condense GENERATION it was asked for. A result that
arrives after a newer condense is discarded, so a summary is only ever spliced
into the breadcrumb that describes the same slice.

The call is part of the turn, not background memory work: it is exempt from
the interactive yield window and from ``abort_background`` (the turn's own
next send would otherwise cancel it). It still counts against the compaction
rate category.
"""
from __future__ import annotations

import collections
import threading
import time

_LOCK = threading.Lock()
#: key -> (generation, text, finished_at). Bounded: a run that ends without
#: calling :func:`release` (a crashed stream) must not grow this forever.
_READY: "collections.OrderedDict[str, tuple[int, str, float]]" = \
    collections.OrderedDict()
#: key -> (generation, cancel event) of the call in flight.
_INFLIGHT: "dict[str, tuple[int, threading.Event]]" = {}
_READY_MAX = 64
_READY_TTL_S = 3600.0


def _prune_locked(now: float) -> None:
    for k in [k for k, v in _READY.items() if now - v[2] > _READY_TTL_S]:
        _READY.pop(k, None)
    while len(_READY) > _READY_MAX:
        _READY.popitem(last=False)


def take(key: "str | None", gen: int) -> str:
    """The finished summary for generation ``gen`` of run ``key``, or "".

    A result for any other generation is stale and is dropped."""
    if not key:
        return ""
    with _LOCK:
        entry = _READY.pop(key, None)
        if entry is None:
            return ""
        if entry[0] != gen:
            return ""
        return entry[1]


def pending(key: "str | None") -> "int | None":
    """Generation of the summary in flight for ``key``, or None."""
    with _LOCK:
        cur = _INFLIGHT.get(key or "")
        return cur[0] if cur else None


def release(key: "str | None") -> None:
    """The run is over: cancel its summary and forget any result."""
    if not key:
        return
    with _LOCK:
        cur = _INFLIGHT.pop(key, None)
        _READY.pop(key, None)
    if cur is not None:
        cur[1].set()


def reset() -> None:
    """Test helper."""
    with _LOCK:
        evs = [ev for _, ev in _INFLIGHT.values()]
        _INFLIGHT.clear()
        _READY.clear()
    for ev in evs:
        ev.set()


def _call(call, cancel: threading.Event) -> str:
    """Run ``call()`` with this thread's cancel token bound and the priority
    yield turned off. Returns "" on any failure."""
    try:
        from aiforge_core.llm import client as _client
        _client.set_cancel_event(cancel)
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.llm import interactive_gate as _gate
        _gate.set_exempt(True)
    except Exception:  # noqa: BLE001
        pass
    try:
        out = call()
    except Exception:  # noqa: BLE001
        return ""
    return out.strip() if isinstance(out, str) else ""


def schedule(key: "str | None", gen: int, call) -> bool:
    """Start ``call`` for generation ``gen`` of run ``key``; return at once.

    A newer generation cancels the older call rather than waiting for it, so a
    slow model never leaves the breadcrumb one condense behind. Returns False
    when nothing was started (no key, or this generation is already running).
    """
    if not key:
        return False
    with _LOCK:
        cur = _INFLIGHT.get(key)
        if cur is not None and cur[0] == gen:
            return False
        ev = threading.Event()
        _INFLIGHT[key] = (gen, ev)
        stale = _READY.get(key)
        if stale is not None and stale[0] != gen:
            _READY.pop(key, None)
    if cur is not None:
        cur[1].set()

    def _worker() -> None:
        text = _call(call, ev)
        with _LOCK:
            mine = _INFLIGHT.get(key)
            if mine is not None and mine[1] is ev:
                del _INFLIGHT[key]
                if text and not ev.is_set():
                    _READY[key] = (gen, text, time.monotonic())
                    _READY.move_to_end(key)
                    _prune_locked(time.monotonic())

    threading.Thread(target=_worker, daemon=True, name="aiforge-compact").start()
    return True


__all__ = ["pending", "release", "reset", "schedule", "take"]
