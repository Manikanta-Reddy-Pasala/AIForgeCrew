"""Keeping ONE run on a ticket: a claim heartbeat and a per-worktree lock.

Two runners could work the same ticket. The claim lived AIFORGE_TICKET_LEASE_S
(1h) but a pipeline legitimately runs up to its 90-min deadline, and nothing
renewed it — so any runner's startup reaper requeued the LIVE ticket, another
runner claimed it, and its worktree reset (`git reset --hard`, `git clean -fd`)
wiped the first run's in-flight edits. :func:`hold_claim` renews the claim for
as long as the run is alive; a crashed run stops renewing, so its ticket is
still reaped once the lease lapses.

Child tickets of one root share that root's worktree, and each run resets it
on entry. :func:`worktree_lock` lets only one of them hold it at a time, across
processes (flock), so a sibling waits in `todo` instead of wiping the other.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import threading

try:
    import fcntl
except ImportError:          # native Windows: no flock — an in-process lock only
    fcntl = None

from . import store

log = logging.getLogger("aiforge.tickets.lease")


def _interval_s() -> float:
    """Renew well inside the lease: a third of it, at most every 5 min."""
    return max(5.0, min(300.0, store.lease_seconds() / 3))


@contextlib.contextmanager
def hold_claim(ticket_id: int, interval_s: "float | None" = None):
    """Renew ``ticket_id``'s claim every ``interval_s`` until the block exits."""
    stop = threading.Event()
    every = interval_s or _interval_s()

    def _beat():
        while not stop.wait(every):
            try:
                store.renew_claim(ticket_id)
            except Exception as exc:  # noqa: BLE001 — a missed beat is not fatal
                log.debug("claim heartbeat ticket=%s failed: %s", ticket_id, exc)

    t = threading.Thread(target=_beat, name=f"claim-{ticket_id}", daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=5)


def _lock_dir() -> str:
    base = os.environ.get("AIFORGE_CONFIG_DIR") or os.path.expanduser("~/.aiforge")
    d = os.path.join(base, "locks")
    os.makedirs(d, exist_ok=True)
    return d


_THREAD_LOCKS: "dict[str, threading.Lock]" = {}


@contextlib.contextmanager
def worktree_lock(root_identifier: str):
    """Hold the root ticket's worktree exclusively, across processes. Yields
    True when held, False when another run holds it (the caller backs off)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(root_identifier or "none"))
    if fcntl is None:
        lk = _THREAD_LOCKS.setdefault(safe, threading.Lock())
        if not lk.acquire(blocking=False):
            yield False
            return
        try:
            yield True
        finally:
            lk.release()
        return
    path = os.path.join(_lock_dir(), f"worktree-{safe}.lock")
    fh = open(path, "a+")  # noqa: SIM115 — held for the whole block
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def defer(ticket_id: int, seconds: int = 120, reason: str = "") -> None:
    """Put a claimed ticket back to ``todo``, not claimable for ``seconds`` —
    NOT a reclaim. Without the delay the runner would re-claim the same oldest
    ticket on its very next poll and starve every other ticket."""
    import datetime as _dt
    until = (_dt.datetime.now(_dt.timezone.utc)
             + _dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    store.update_status(ticket_id, "todo", role="adk_runner",
                        metadata_patch={"retry_after": until,
                                        "deferred_reason": reason})


__all__ = ["hold_claim", "worktree_lock", "defer"]
