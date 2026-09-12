"""Memory compaction WHENEVER AIForge is idle — and resumable.

Compaction used to run once, at an evening hour: on a box used at odd hours it
ran while the user worked, and a laptop asleep at 18:00 compacted nothing that
day. Now a light check every few minutes asks "is anyone using this?" — no chat
run in flight, no chat activity for ``AIFORGE_COMPACT_IDLE_MIN`` minutes
(default 10), no ticket being worked — and only then compacts.

It YIELDS the moment the user is back: every stage checks ``should_stop()``
between work items (a session, a brief group), so a chat never queues behind a
fold for long. Progress is saved to ``compaction-progress.json`` in the config
dir: the next idle window resumes where the last one stopped — stages already
done are skipped, and inside the full re-fold every brief group already folded
is skipped — across restarts too.

One full cycle (sessions → briefs → full re-fold) at most every
``AIFORGE_COMPACT_CYCLE_H`` hours (default 24); a new cycle starts only once
the previous one completed. This is the default schedule; an explicit
``AIFORGE_COMPACT_AT_HOUR`` (or ``AIFORGE_COMPACT_EVERY_H``) keeps the older
fixed-hour / hourly ones (runtime.compact_window.idle_mode).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir

log = logging.getLogger("aiforge.compact_idle")
_LOCK = threading.Lock()
_MAX_STAGE_FAILURES = 3


def _env_float(key: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(key, default)))
    except (TypeError, ValueError):
        return default


def idle_after_s() -> float:
    return _env_float("AIFORGE_COMPACT_IDLE_MIN", 10.0) * 60


def cycle_every_s() -> float:
    return _env_float("AIFORGE_COMPACT_CYCLE_H", 24.0) * 3600


def _tickets_in_progress() -> bool:
    try:
        from aiforge_core.tickets import store
        return bool(store.list_tickets(statuses=["in_progress"]))
    except Exception:  # noqa: BLE001 — unknown counts as not busy
        return False


def user_active() -> bool:
    """Someone is using AIForge: a chat run in flight, chat activity within the
    idle window, or a ticket being worked."""
    try:
        from aiforge_core.runtime import chat_runs
        if chat_runs.any_active():
            return True
        if time.time() - chat_runs.last_activity() < idle_after_s():
            return True
    except Exception:  # noqa: BLE001
        pass
    return _tickets_in_progress()


def _path() -> str:
    return os.path.join(str(config_dir()), "compaction-progress.json")


class Checkpoint:
    """The saved progress of one compaction cycle."""

    def __init__(self, data: dict) -> None:
        self.data = data

    # -- persistence --------------------------------------------------------
    @classmethod
    def load(cls) -> "Checkpoint":
        try:
            with open(_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return cls(data)
        except (OSError, ValueError):
            pass
        return cls({})

    def save(self) -> None:
        try:
            _atomic.write_text(_path(), json.dumps(self.data, indent=1))
        except Exception as exc:  # noqa: BLE001 — progress is best-effort
            log.debug("compaction progress not saved: %s", exc)

    # -- cycle --------------------------------------------------------------
    def due(self, now: "float | None" = None) -> bool:
        """A cycle is in progress (resume it), or the last one completed long
        enough ago to start the next."""
        now = now or time.time()
        if self.data.get("started_at") and not self.data.get("completed_at"):
            return True
        done = float(self.data.get("completed_at") or 0)
        return now - done >= cycle_every_s()

    def begin(self) -> None:
        if not self.data.get("started_at") or self.data.get("completed_at"):
            self.data = {"started_at": time.time(), "completed_at": None,
                         "stages": [], "steps": [], "groups": {}}
            self.save()

    def complete(self) -> None:
        self.data["completed_at"] = time.time()
        self.save()

    # -- stages / steps / groups -------------------------------------------
    def stage_done_already(self, name: str) -> bool:
        return name in self.data.get("stages", [])

    def stage_done(self, name: str) -> None:
        self.data.setdefault("stages", []).append(name)
        self.save()

    def step_done_already(self, name: str) -> bool:
        return name in self.data.get("steps", [])

    def step_done(self, name: str) -> None:
        self.data.setdefault("steps", []).append(name)
        self.save()

    def groups_done(self, axis: str) -> set:
        return set(self.data.get("groups", {}).get(axis, []))

    def group_done(self, axis: str, key: str) -> None:
        self.data.setdefault("groups", {}).setdefault(axis, []).append(key)
        self.save()

    @staticmethod
    def should_stop() -> bool:
        return user_active()


def run_when_idle(stages) -> str:
    """Run (or resume) a compaction cycle if AIForge is idle and one is due.
    ``stages`` is ``[(name, fn(checkpoint) -> "done" | "stopped" | "failed")]``.
    Returns what happened: "busy", "not-due", "stopped", "failed" or "done"."""
    if not _LOCK.acquire(blocking=False):
        return "busy"                       # a pass is already running
    try:
        if user_active():
            return "busy"
        cp = Checkpoint.load()
        if not cp.due():
            return "not-due"
        cp.begin()
        for name, fn in stages:
            if cp.stage_done_already(name):
                continue
            if user_active():
                log.info("idle compaction: user is back — pausing before %s", name)
                return "stopped"
            outcome = fn(cp)
            if outcome == "stopped":
                log.info("idle compaction: paused inside %s — resumes next idle window", name)
                return "stopped"
            if outcome == "failed":
                fails = cp.data.setdefault("failures", {})
                fails[name] = int(fails.get(name, 0)) + 1
                if fails[name] < _MAX_STAGE_FAILURES:
                    cp.save()
                    log.warning("idle compaction: stage %s failed (%d) — retried "
                                "next idle window", name, fails[name])
                    return "failed"
                # A stage that keeps failing must not spend model calls every
                # idle window forever: skip it for the rest of this cycle.
                log.warning("idle compaction: stage %s failed %d times — skipped "
                            "until the next cycle", name, fails[name])
            cp.stage_done(name)
        cp.complete()
        log.info("idle compaction: cycle complete")
        return "done"
    finally:
        _LOCK.release()


def state() -> dict:
    """For Settings / status: the saved progress and whether it would run now."""
    cp = Checkpoint.load()
    return {**cp.data, "due": cp.due(), "user_active": user_active(),
            "idle_after_min": idle_after_s() / 60,
            "cycle_every_h": cycle_every_s() / 3600}


__all__ = ["Checkpoint", "run_when_idle", "user_active", "state"]
