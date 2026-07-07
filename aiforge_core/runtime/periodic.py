"""ONE engine for internal recurring maintenance tasks (daily reindex, chat-md
compaction, graph dedup, …).

Before this there were several ad-hoc `while True: sleep(); do_thing()` loops,
each with its own error handling and no shared "fire at most once per slot"
logic. Register a task here and it runs on the single periodic loop, launched
through `background.spawn` (name + error sink) with a per-task debounce so a
manual trigger + the scheduled fire don't double-run.

    periodic.register("daily-reindex", _spawn_reindex_all, at_hour=3)
    periodic.register("chat-compact", compact_chats, every_s=6*3600)

Not a distributed scheduler (that's `jobs/scheduler.py` for user cron jobs, with
`store.claim` at-most-once). This is in-process maintenance; for multi-replica
add a claim-key later. Off entirely with AIFORGE_PERIODIC_DISABLE=1.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

log = logging.getLogger("aiforge.periodic")


@dataclass
class _Task:
    name: str
    fn: "Callable[[], object]"
    every_s: "float | None" = None      # fixed interval
    at_hour: "int | None" = None        # daily at local hour [0-23]
    debounce_s: float = 60.0
    _last: list = field(default_factory=lambda: [0.0])   # monotonic ts

    def _next_after(self, now_mono: float, now_dt: datetime) -> float:
        """Seconds until this task is next due (from now)."""
        if self.at_hour is not None:
            nxt = now_dt.replace(hour=self.at_hour, minute=0, second=0,
                                 microsecond=0)
            if nxt <= now_dt:
                nxt += timedelta(days=1)
            return (nxt - now_dt).total_seconds()
        if self.every_s:
            elapsed = now_mono - self._last[0]
            return max(0.0, self.every_s - elapsed)
        return float("inf")


_TASKS: "list[_Task]" = []
_started = False
_lock = threading.Lock()


def register(name: str, fn: "Callable[[], object]", *,
             every_s: "float | None" = None, at_hour: "int | None" = None,
             debounce_s: float = 60.0) -> None:
    """Register a recurring task. Give EITHER ``every_s`` OR ``at_hour``."""
    with _lock:
        if any(t.name == name for t in _TASKS):
            return
        _TASKS.append(_Task(name=name, fn=fn, every_s=every_s, at_hour=at_hour,
                            debounce_s=debounce_s))


def _fire(t: _Task) -> None:
    now = time.monotonic()
    if now - t._last[0] < t.debounce_s:
        return
    t._last[0] = now
    from aiforge_core.runtime import background as _bg
    _bg.spawn(t.fn, name=f"periodic:{t.name}")
    log.info("periodic fired: %s", t.name)


def start() -> None:
    """Launch the single periodic loop (idempotent). No-op when disabled or no
    tasks are registered."""
    global _started
    if os.environ.get("AIFORGE_PERIODIC_DISABLE", "") in ("1", "true", "yes"):
        return
    with _lock:
        if _started or not _TASKS:
            return
        _started = True

    def _loop() -> None:
        while True:
            now_mono, now_dt = time.monotonic(), datetime.now()
            # sleep until the SOONEST due task, capped so at_hour tasks are
            # re-evaluated and new registrations get picked up.
            wait = min((t._next_after(now_mono, now_dt) for t in _TASKS),
                       default=3600.0)
            # Floor keeps the loop cheap when idle (daily tasks → capped at 1h so
            # new registrations + at_hour re-eval happen); a task due sooner still
            # fires within ~1s.
            time.sleep(max(1.0, min(wait, 3600.0)))
            nm = time.monotonic()
            nd = datetime.now()
            for t in list(_TASKS):
                if t._next_after(nm, nd) <= 1.0:
                    try:
                        _fire(t)
                    except Exception as exc:  # noqa: BLE001 — never kill the loop
                        log.warning("periodic task %s fire failed: %s", t.name, exc)

    from aiforge_core.runtime import background as _bg
    _bg.spawn(_loop, name="periodic-loop")


__all__ = ["register", "start"]
