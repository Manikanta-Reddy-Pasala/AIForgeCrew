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
    """Renew ``ticket_id``'s claim every ``interval_s`` until the block exits.

    The beat runs on its own thread, so a run that is WAITING for the model
    (llm/model_wait — unbounded by default) keeps its claim: it is never reaped
    or run twice, and the reclaim cap never sees the wait. When a beat finds the
    ticket no longer ``in_progress`` (cancelled, or taken over), the claim is
    LOST: every model wait inside the block ends (ModelWaitCancelled) instead
    of waiting for a model on behalf of a run nobody wants any more. The block
    also reports each wait's status on the ticket as an ``llm_wait`` event.

    The claim is THIS run's: the beat renews it only while ``claimed_at`` is
    still the value this run set (a token), so a ticket reaped and claimed by
    another runner reads as taken over, not as renewed. Yields the ``lost``
    event — a cancel check for work the run hands to other threads."""
    stop = threading.Event()
    lost = threading.Event()
    every = interval_s or _interval_s()
    token = [_claim_token(ticket_id)]

    def _beat():
        while not stop.wait(every):
            try:
                renewed = (store.renew_claim(ticket_id, token[0]) if token[0]
                           else store.renew_claim(ticket_id))
                if renewed is False or renewed is None:
                    lost.set()
                elif isinstance(renewed, str):
                    token[0] = renewed
            except Exception as exc:  # noqa: BLE001 — a missed beat is not fatal
                log.debug("claim heartbeat ticket=%s failed: %s", ticket_id, exc)

    t = threading.Thread(target=_beat, name=f"claim-{ticket_id}", daemon=True)
    t.start()
    owner = _jobs_owner(ticket_id)
    _stop_jobs(owner)            # a reclaim: the last attempt's leftovers go
    tok = _own_jobs(owner)
    from aiforge_core.llm import model_wait
    try:
        with model_wait.scope(lost, "ticket claim lost (cancelled or taken "
                                    "over)"), \
                model_wait.status_sink(_wait_event(ticket_id)):
            yield lost
    finally:
        stop.set()
        t.join(timeout=5)
        _disown_jobs(tok)
        _stop_jobs(owner)        # this attempt's commands end with its claim


def _claim_token(ticket_id) -> "str | None":
    """The claim this run holds (its ``claimed_at``), or None when unknown."""
    try:
        row = store.get_backend().fetch_ticket(int(ticket_id))
        return str(row.get("claimed_at")) if row and row.get("claimed_at") \
            else None
    except Exception:  # noqa: BLE001 — unknown: renew by status alone
        return None


def _wait_event(ticket_id):
    """A model_wait status sink that records the status on the ticket."""
    def _sink(st: dict) -> None:
        try:
            store.add_event(ticket_id, "llm", "llm_wait", st.get("text", ""),
                            {k: v for k, v in st.items() if k != "text"})
        except Exception as exc:  # noqa: BLE001 — status is best-effort
            log.debug("llm_wait event ticket=%s failed: %s", ticket_id, exc)
    return _sink


def _jobs_owner(ticket_id) -> str:
    return f"ticket-{ticket_id}"


def _own_jobs(owner):
    """Tag every command this claim's run starts, so the claim can stop them."""
    try:
        from aiforge_core.runtime import cmd_jobs
        return cmd_jobs.set_owner(owner)
    except Exception:  # noqa: BLE001
        return None


def _disown_jobs(tok) -> None:
    if tok is None:
        return
    try:
        from aiforge_core.runtime import cmd_jobs
        cmd_jobs.reset_owner(tok)
    except Exception:  # noqa: BLE001
        pass


def _stop_jobs(owner) -> None:
    """Stop the commands ``owner`` still has running — in this process (the
    job table) and from an earlier, crashed one (bg_work's saved rows)."""
    try:
        from aiforge_core.runtime import bg_work, cmd_jobs
        n = cmd_jobs.end_owner(owner) + bg_work.stop_owner(owner)
        if n:
            log.info("stopped %d leftover command(s) of %s", n, owner)
    except Exception as exc:  # noqa: BLE001
        log.debug("stopping %s commands failed: %s", owner, exc)


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
