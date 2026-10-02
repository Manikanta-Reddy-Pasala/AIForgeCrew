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

import os
import queue
import threading
import time
from typing import Any

from aiforge_core.runtime.chat_event_slim import slim_event

# Sentinel pushed onto every subscriber queue when a run completes, so a
# tailing consumer knows to stop without polling ``done``.
_SENTINEL = object()


class _Run:
    """One in-flight chat run for a session."""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        self.events: list[dict] = []          # full ordered buffer (replay)
        # Streamed text of the model call in flight, merged per phase. Not in
        # `events`: a long build streams thousands of chunks, and every one is
        # superseded by the step/answer event that settles its call — so only
        # this unsettled tail is worth replaying to a re-attaching client.
        self.pending_deltas: list[dict] = []
        self.subscribers: set[queue.Queue] = set()
        self.done = False
        # The turn's `done` went out — only saving the turn and the post-answer
        # work (next-step suggestion, learning) remain. A new message arriving
        # now waits for that instead of a 409 (see settle()).
        self.answered = False
        self.finished = threading.Event()
        self.started_at = time.time()         # epoch secs — for reattach timer
        self.lock = threading.Lock()
        # When the run last SAID anything, and what it is doing — so silence can
        # be told apart from work and named ("waiting for the model", "running
        # pytest") instead of leaving a timer counting over nothing.
        self.last_event_at = time.time()
        self.phase = ""
        self.quiet_notices = 0                # how many quiet notices went out
        self.worker: "threading.Thread | None" = None   # the producer thread
        # What the run is doing right now and what it has done, kept as events
        # go by so "what is the status?" can be answered from the run itself,
        # instantly, without asking a model.
        self.open_tools: dict = {}            # call_id -> {name, args, at}
        self.recent_tools: list = []          # last few finished tool calls
        self.tool_count = 0
        self.last_thought = ""
        self.last_notice = ("", 0.0)          # newest model-wait line + when
        self.changes: dict = {}               # files / additions / deletions

    # -- producer side -------------------------------------------------------

    def publish(self, event: dict) -> None:
        with self.lock:
            if self.done:
                return
            # Every event is ACTIVITY. Touching only on start/finish left a long
            # turn looking idle from the moment it began, so the idle compactor
            # (which treats "no chat activity for N minutes" as nobody home)
            # started folding memory in the middle of a run that was still
            # calling tools. Cheap: one float assignment per event.
            _touch()
            # Don't buffer heartbeats — iter_subscription generates its own per
            # subscriber. Buffering the producer's pings would replay a growing
            # pile of them to every re-attach. Forward live but don't store.
            kind = event.get("type")
            if kind != "ping" and not event.get("quiet_notice"):
                self.last_event_at = time.time()
                self.quiet_notices = 0
                self.phase = _phase_after(event, self.phase)
                self._track(event)
            if kind == "done":
                self.answered = True
            if kind == "delta":
                self._hold_delta(event)
            elif kind != "ping":
                self.pending_deltas = []      # this event settles the stream
                # A replay needs the row, not an 80 KB read result; an
                # hours-long run would otherwise hold every one in memory.
                self.events.append(slim_event(event))
            for q in self.subscribers:
                q.put(event)

    def _track(self, event: dict) -> None:
        """Fold one event into the live picture. Never raises."""
        try:
            kind = event.get("type")
            now = time.time()
            if kind == "tool_start":
                key = event.get("call_id", len(self.open_tools) + self.tool_count)
                self.open_tools[key] = {"name": event.get("name") or "a tool",
                                        "args": short_args(event.get("args")),
                                        "at": now}
                while len(self.open_tools) > 12:      # a leaked start never grows
                    self.open_tools.pop(next(iter(self.open_tools)))
            elif kind == "tool":
                started = self.open_tools.pop(event.get("call_id"), None)
                if started is None and len(self.open_tools) == 1 \
                        and event.get("call_id") is None:
                    started = self.open_tools.pop(next(iter(self.open_tools)))
                res = event.get("result")
                failed = isinstance(res, dict) and (res.get("ok") is False
                                                    or bool(res.get("error")))
                self.tool_count += 1
                self.recent_tools.append({
                    "name": event.get("name") or (started or {}).get("name") or "tool",
                    "args": short_args(event.get("args")) or (started or {}).get("args", ""),
                    "ok": not failed,
                    "secs": round(now - started["at"], 1) if started else None})
                del self.recent_tools[:-6]
            elif kind == "thought":
                text = str(event.get("text") or "")
                if event.get("role") == "system":
                    if text[:1] in ("⏸", "⟳", "⚠", "⏳", "▶"):
                        self.last_notice = (text, now)
                elif text.strip():
                    self.last_thought = " ".join(text.split())[:200]
            elif kind == "changes":
                files = event.get("files") or []
                summ = event.get("summary") or {}
                self.changes = {"files": summ.get("files", len(files)),
                                "additions": summ.get("additions", 0),
                                "deletions": summ.get("deletions", 0)}
            elif kind in ("message", "done", "error"):
                self.open_tools.clear()
        except Exception:  # noqa: BLE001 — tracking never breaks a run
            pass

    def _hold_delta(self, event: dict) -> None:
        """Keep the in-flight call's stream as one event per phase run."""
        if event.get("phase") == "reset":
            self.pending_deltas = [event]
            return
        last = self.pending_deltas[-1] if self.pending_deltas else None
        if (last is not None and last.get("phase") == event.get("phase")
                and last.get("role") == event.get("role")):
            self.pending_deltas[-1] = {**last, "text": (last.get("text") or "")
                                       + (event.get("text") or "")}
        else:
            self.pending_deltas.append(dict(event))

    def finish(self) -> None:
        with self.lock:
            first = not self.done
            self.done = True
            self.finished.set()
            for q in self.subscribers:
                q.put(_SENTINEL)
        _touch()
        if first:
            _run_finish_hooks(self.session_id)

    # -- consumer side -------------------------------------------------------

    def subscribe(self) -> queue.Queue:
        """Register a tail and pre-load it with everything buffered so far.

        Snapshot + register happen under the lock so no event can slip
        between the replay copy and the live subscription (no gap, no dupe).
        """
        q: queue.Queue = queue.Queue()
        with self.lock:
            for ev in self.events + self.pending_deltas:
                q.put(ev)
            if self.done:
                q.put(_SENTINEL)
            else:
                self.subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subscribers.discard(q)


def short_args(args) -> str:
    """One short line naming what a tool call is about."""
    if not isinstance(args, dict) or not args:
        return ""
    for key in ("cmd", "command", "path", "file", "query", "pattern", "url", "id"):
        if args.get(key):
            # A command keeps more: it is shortened by the reader, and cutting
            # it at 80 characters left "…/ai_elint && .aiforge-venv/bi".
            limit = 240 if key in ("cmd", "command") else 80
            return " ".join(str(args[key]).split())[:limit]
    first = next(iter(args.values()))
    return " ".join(str(first).split())[:80]


def _phase_after(event: dict, current: str) -> str:
    """What the run is doing once ``event`` has gone out."""
    kind = event.get("type")
    if kind == "tool_start":
        return f"running {event.get('name') or 'a tool'}"
    if kind == "approval":
        return "waiting for your approval"
    if kind == "delta":
        return "the model is writing"
    if kind in ("tool", "approval_expired", "auto_approved"):
        return "waiting for the model"
    if kind in ("message", "done", "error"):
        return "finishing up"
    return current


def set_phase(session_id, text: str) -> None:
    """Name what the session's run is about to do. For stretches that emit no
    event of their own (a git snapshot, building context, the first model
    call) — so a quiet spell can say what it is waiting on."""
    if session_id is None:
        return
    run = get(session_id)
    if run is not None and not run.done:
        run.phase = text or ""


def _fmt_quiet(seconds: float) -> str:
    s = int(seconds)
    return f"{s}s" if s < 90 else f"{s // 60}m"


def _quiet_marks() -> list[float]:
    """Seconds of silence at which a run says so (``AIFORGE_CHAT_QUIET_NOTICE_S``,
    comma separated; the last gap repeats). Empty or 0 turns the notices off."""
    raw = os.environ.get("AIFORGE_CHAT_QUIET_NOTICE_S", "60,180,600")
    out: list[float] = []
    for part in raw.split(","):
        try:
            v = float(part)
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return sorted(out)


def _next_quiet_mark(marks: list[float], sent: int) -> float:
    if sent < len(marks):
        return marks[sent]
    step = marks[-1] - marks[-2] if len(marks) > 1 else marks[-1]
    return marks[-1] + max(step, 60.0) * (sent - len(marks) + 1)


def _check_run(run: "_Run", now: float, marks: list[float]) -> None:
    """One watchdog look at one live run."""
    quiet = now - run.last_event_at
    worker = run.worker
    if worker is not None and not worker.is_alive() and quiet > 5.0:
        # The thread that was producing this turn is gone and never closed the
        # run: nothing is running, whatever the UI's timer says.
        run.publish({"type": "error",
                     "text": "This run stopped without finishing (its worker "
                             "ended unexpectedly). Nothing is running now — "
                             "send the message again."})
        run.publish({"type": "done"})
        run.finish()
        return
    if marks and quiet >= _next_quiet_mark(marks, run.quiet_notices):
        run.quiet_notices += 1
        doing = run.phase or "still working"
        run.publish({"type": "thought", "role": "system", "quiet_notice": True,
                     "text": f"⏳ No output for {_fmt_quiet(quiet)} — {doing}. "
                             "The run is alive; Stop ends it."})


def _watch() -> None:
    while True:
        time.sleep(5.0)
        try:
            marks = _quiet_marks()
            with _LOCK:
                live = [r for r in _RUNS.values() if not r.done]
            now = time.time()
            for run in live:
                _check_run(run, now, marks)
        except Exception:  # noqa: BLE001 — the watchdog must outlive any run
            pass


_WATCH_STARTED = False


def _ensure_watchdog() -> None:
    global _WATCH_STARTED
    if _WATCH_STARTED:
        return
    _WATCH_STARTED = True
    threading.Thread(target=_watch, name="chat-run-watchdog", daemon=True).start()


# Called (each on its own daemon thread) with the session id when a run ends —
# after the turn is persisted. How side tasks learn that a run finished without
# polling. A hook that raises is ignored.
_FINISH_HOOKS: list = []


def on_finish(fn) -> None:
    """Register ``fn(session_id)`` to run whenever a chat run finishes."""
    if fn not in _FINISH_HOOKS:
        _FINISH_HOOKS.append(fn)


def _run_finish_hooks(session_id: int) -> None:
    for fn in list(_FINISH_HOOKS):
        def _call(fn=fn):
            try:
                fn(session_id)
            except Exception:  # noqa: BLE001 — a hook never affects a run
                pass
        threading.Thread(target=_call, name="chat-run-finished",
                         daemon=True).start()


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
    for sid in tuple(_RUNS):
        if len(_RUNS) <= _MAX_RUNS:
            break
        if _RUNS[sid].done:
            del _RUNS[sid]


# When a chat run last started or ended — the idle compactor's "is anyone
# using this?" signal (runtime.compact_idle).
_LAST_ACTIVITY = [0.0]


def _touch() -> None:
    _LAST_ACTIVITY[0] = time.time()


def last_activity() -> float:
    """Epoch seconds of the last chat run start/finish (0 = none yet)."""
    return _LAST_ACTIVITY[0]


def any_active() -> bool:
    """Whether any chat run is in flight right now."""
    with _LOCK:
        return any(not r.done for r in _RUNS.values())


def start(session_id: int) -> _Run:
    """Register a fresh run for ``session_id``, replacing any prior one."""
    _touch()
    _ensure_watchdog()
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


def settle(session_id: int, timeout: float = 30.0) -> bool:
    """True when the session has no run in flight — waiting up to ``timeout``
    for one that already ANSWERED (sent `done`) to finish its bookkeeping. The
    UI's "Approve & Execute" and a quick follow-up used to hit a 409 in that
    gap. False while a run is genuinely still working (or it overruns)."""
    run = get(session_id)
    if run is None or run.done:
        return True
    if not run.answered:
        return False
    return run.finished.wait(timeout)


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
                # The heartbeat says how long the run has been quiet and what
                # it is doing, so the UI can show that instead of a bare timer.
                yield {"type": "ping",
                       "quiet_s": int(max(0.0, time.time() - run.last_event_at)),
                       "phase": run.phase}
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
