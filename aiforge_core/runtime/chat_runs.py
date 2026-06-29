"""Live chat-run registry — makes an in-flight chat turn survive the client
navigating away and back.

WHY THIS EXISTS
---------------
Simple/plan-mode chat used to run the agent loop INLINE inside the
StreamingResponse generator of ``POST /chat/sessions/{id}/message``. When the
user left the Chat view the React component unmounted and aborted the fetch,
which closed that generator — killing the run mid-flight and persisting only a
partial (often empty) assistant turn. The user lost the run's progress AND the
next turn's history was missing the killed reply, so the agent "forgot" what it
had just been doing.

The team-mode pipeline already dodged this by running its work on a background
daemon thread and persisting from there. This module generalises that pattern
to EVERY mode:

* the producer (the event-emitting generator) runs on a background daemon
  thread and publishes each event here;
* every event is appended to a per-session buffer AND fanned out to any live
  subscriber queues;
* the HTTP request just SUBSCRIBES and tails the buffer — so a client
  disconnect only drops that subscriber, never the producer;
* a returning client re-attaches via ``GET /chat/sessions/{id}/attach``, which
  replays the buffer (rebuilding the live turn) and then tails live events.

Process-local (single-worker uvicorn, matching the rest of the chat runtime —
chat_cancel / chat_approve / chat_interject are all in-process too).
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any

# Sentinel pushed onto every subscriber queue when a run completes, so a
# tailing consumer knows to stop without polling ``done``.
_SENTINEL = object()


class _Run:
    """One in-flight chat run for a session."""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        self.events: list[dict] = []          # full ordered buffer (replay)
        self.subscribers: set[queue.Queue] = set()
        self.done = False
        self.started_at = time.time()         # epoch secs — for reattach timer
        self.lock = threading.Lock()

    # -- producer side -------------------------------------------------------

    def publish(self, event: dict) -> None:
        with self.lock:
            if self.done:
                return
            # Don't buffer heartbeats — iter_subscription generates its own per
            # subscriber. Buffering the producer's pings would replay a growing
            # pile of them to every re-attach. Forward live but don't store.
            if event.get("type") != "ping":
                self.events.append(event)
            for q in self.subscribers:
                q.put(event)

    def finish(self) -> None:
        with self.lock:
            self.done = True
            for q in self.subscribers:
                q.put(_SENTINEL)

    # -- consumer side -------------------------------------------------------

    def subscribe(self) -> queue.Queue:
        """Register a tail and pre-load it with everything buffered so far.

        Snapshot + register happen under the lock so no event can slip
        between the replay copy and the live subscription (no gap, no dupe).
        """
        q: queue.Queue = queue.Queue()
        with self.lock:
            for ev in self.events:
                q.put(ev)
            if self.done:
                q.put(_SENTINEL)
            else:
                self.subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subscribers.discard(q)


_LOCK = threading.Lock()
_RUNS: dict[int, _Run] = {}
# Keep at most this many runs in the registry. A finished run's buffer lingers
# so a client returning right at the finish line can still replay it; this caps
# how many such buffers accumulate in a long-lived server. Live runs are never
# evicted — only the oldest FINISHED ones once over the cap.
_MAX_RUNS = 64


def _prune_locked() -> None:
    if len(_RUNS) <= _MAX_RUNS:
        return
    # Evict finished runs in insertion order (oldest first) until under cap.
    for sid in list(_RUNS):
        if len(_RUNS) <= _MAX_RUNS:
            break
        if _RUNS[sid].done:
            del _RUNS[sid]


def start(session_id: int) -> _Run:
    """Register a fresh run for ``session_id``, replacing any prior one."""
    with _LOCK:
        run = _Run(session_id)
        _RUNS[session_id] = run
        _prune_locked()
        return run


def get(session_id: int) -> _Run | None:
    with _LOCK:
        return _RUNS.get(session_id)


def is_running(session_id: int) -> bool:
    run = get(session_id)
    return bool(run and not run.done)


def publish(session_id: int, event: dict) -> None:
    run = get(session_id)
    if run is not None:
        run.publish(event)


def finish(session_id: int) -> None:
    """Mark the run done and wake every subscriber. Keeps the buffer around so
    a client that re-attaches right at the finish line still replays it; the
    next ``start()`` for the session evicts it."""
    run = get(session_id)
    if run is not None:
        run.finish()


def finish_all() -> list[int]:
    """Finish (wake + close) every run. Part of the kill-all reset so no
    subscriber tails a run whose producer is being torn down. Returns the ids."""
    with _LOCK:
        runs = list(_RUNS.items())
    for _sid, run in runs:
        run.finish()
    return [sid for sid, _ in runs]


def subscribe(session_id: int) -> queue.Queue | None:
    """Tail an active run (replay buffer, then live). ``None`` if no run."""
    run = get(session_id)
    if run is None:
        return None
    return run.subscribe()


def unsubscribe(session_id: int, q: queue.Queue) -> None:
    run = get(session_id)
    if run is not None:
        run.unsubscribe(q)


def iter_subscription(run: "_Run", q: queue.Queue,
                      ping_every: float = 10.0) -> Any:
    """Yield live events for a subscriber queue until the run ends.

    Takes the captured ``_Run`` (NOT a session id) so cleanup unsubscribes from
    the exact run the queue belongs to — a newer run may have replaced this one
    in the registry, and unsubscribing by id would leak the queue on the old
    run (it would keep ``put``-ing into an orphaned queue forever).

    Emits a ``{"type": "ping"}`` heartbeat on idle so a slow local model can't
    let the SSE connection idle out behind a proxy. The terminal sentinel ends
    the generator (the producer already forwards a real ``done`` event before
    finishing, so callers needn't synthesise one)."""
    try:
        while True:
            try:
                item = q.get(timeout=ping_every)
            except queue.Empty:
                yield {"type": "ping"}
                continue
            if item is _SENTINEL:
                return
            yield item
    finally:
        run.unsubscribe(q)


__all__ = [
    "start", "get", "is_running", "publish", "finish", "finish_all",
    "subscribe", "unsubscribe", "iter_subscription",
]
