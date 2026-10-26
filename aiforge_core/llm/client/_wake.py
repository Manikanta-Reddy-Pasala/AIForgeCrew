"""Cut model calls that a machine sleep left on a dead connection.

A laptop that sleeps under a running turn (the screen locked, the keep-awake
hold missing) pauses the whole Docker VM. On wake, the model request that was
in flight sits on a TCP connection the server or a NAT box dropped long ago,
and nothing arrives on it: the turn waited out the stream bounds — minutes,
more for a big prompt, doubled on every retry — before it moved again.

A watcher thread ticks every ``_TICK_S``. A tick that comes much later than it
should (by wall clock or by the monotonic clock, whichever a paused VM
advances) means the machine slept. Every model connection open at that
moment is shut down at once, and the call fails as a stalled stream, which
the retry path sends again right away on a fresh connection.

``AIFORGE_LLM_WAKE_GAP_S`` (default 60) is the gap that counts as a sleep;
0 turns this off. A forward clock step or a scheduler stall that long cuts
the calls too: wasted work, never a wrong answer (they are sent again).

Only the cancellable path (``_post_cancellable_once``, every chat turn and
any call with a cancel token) is watched; a call without one goes through
urllib and still relies on its own read timeout.
"""
from __future__ import annotations

import itertools
import socket
import threading
import time

from ._helpers import _float_env, _log

_TICK_S = 5.0
_LOCK = threading.Lock()
#: token → (connection, [gap seconds once cut by a wake, else None])
_OPEN: dict[int, tuple] = {}
_IDS = itertools.count(1)
_STARTED = [False]


def _gap_s() -> float:
    return max(0.0, _float_env("AIFORGE_LLM_WAKE_GAP_S", 60.0))


def _cut(conn) -> None:
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)   # wakes a recv blocked in another thread
        except OSError:
            pass
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def woke(gap: float) -> int:
    """The machine slept for about ``gap`` seconds: cut every open model
    connection. Returns how many were cut."""
    with _LOCK:
        hit = [(c, box) for c, box in _OPEN.values() if box[0] is None]
        for _c, box in hit:
            box[0] = gap
    for c, _box in hit:
        _cut(c)
    if hit:
        _log.warning("llm: the machine slept ~%.0fs — %d model call(s) cut "
                     "and sent again", gap, len(hit))
    return len(hit)


def _watch(_sleep=time.sleep, _now=time.time, _mono=time.monotonic) -> None:
    # The clocks are bound here, so a test faking them for its own call never
    # reaches the live watcher thread.
    wall, mono = _now(), _mono()
    while True:
        _sleep(_TICK_S)
        now_w, now_m = _now(), _mono()
        late = max(now_w - wall, now_m - mono) - _TICK_S
        wall, mono = now_w, now_m
        limit = _gap_s()
        if limit and late >= limit:
            try:
                woke(late)
            except Exception:  # noqa: BLE001 — the watcher must keep running
                pass


def _ensure_watcher() -> None:
    if _STARTED[0]:
        return
    with _LOCK:
        if _STARTED[0]:
            return
        _STARTED[0] = True
    threading.Thread(target=_watch, daemon=True, name="llm-wake-watch").start()


def track(conn) -> "int | None":
    """Watch ``conn`` while a model call uses it; the token for
    :func:`untrack` / :func:`cut_by_wake`."""
    if not _gap_s():
        return None
    _ensure_watcher()
    tok = next(_IDS)
    with _LOCK:
        _OPEN[tok] = (conn, [None])
    return tok


def cut_by_wake(tok) -> "float | None":
    """The sleep gap when a wake cut this call's connection, else None."""
    if tok is None:
        return None
    with _LOCK:
        got = _OPEN.get(tok)
    return got[1][0] if got else None


def untrack(tok) -> None:
    if tok is not None:
        with _LOCK:
            _OPEN.pop(tok, None)


__all__ = ["cut_by_wake", "track", "untrack", "woke"]
