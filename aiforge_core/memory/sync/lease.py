"""The compaction lease — the only part of sync that needs a leader.

Replication needs no leader at all. Compaction, OKF node deduplication and
distillation do, because they are LLM-expensive and non-deterministic: two peers
running them concurrently produce different answers from the same input.

This is deliberately not a consensus protocol. If two peers both believe they
hold the lease, both compact; both briefs are content-addressed class A files,
so both land and the next concept-similarity dedupe pass merges them. The cost
of split-brain is wasted tokens, never corruption — which is exactly why Raft
would be more code than the entire rest of this design.

Claim protocol: write the lease with ``rev + 1``, wait one full sync interval,
then read it back. Still holding it? You are the leader. That wait is what
replaces consensus.

Wall-clock is used here, and only here, in the whole sync design. Every other
record orders on a ``rev`` counter precisely because peers' clocks disagree.
The lease reads ``time.time()`` for expiry instead; its failure mode under
clock skew is a spurious double-claim, i.e. duplicate compaction — tolerated
by design (see module docstring above), not a reason to "fix" this into a
timestamp comparison anywhere else in the design.
"""
from __future__ import annotations

import logging
import threading
import time

from aiforge_core.memory.sync import _io, merge, paths
from aiforge_core.memory.sync.paths import LEASE_KEY

_log = logging.getLogger("aiforge.sync")

TTL = 600          # 10 minutes
RENEW_EVERY = 180  # 3 minutes

_heartbeat: threading.Thread | None = None


def read() -> dict:
    """The current lease record, or {} if there is none."""
    return _io.read_json(paths.lease_path())


def _write(rec: dict) -> None:
    _io.write_json(paths.lease_path(), rec)


def _expired(rec: dict) -> bool:
    # as_rev, not int(): the lease record arrives from peers like any other class
    # B record, so a malformed expires_at must read as "long expired" rather than
    # raise and take the caller down.
    return merge.as_rev(rec.get("expires_at")) <= int(time.time())


def claim() -> bool:
    """Take the lease if it is free or expired. Returns whether we now hold it."""
    from aiforge_core.memory.sync.identity import self_id

    me = self_id()
    rec = read()
    if rec and not _expired(rec) and rec.get("holder") != me:
        return False
    now = int(time.time())
    _write({
        "origin": "",
        "key": LEASE_KEY,
        "rev": merge.as_rev(rec.get("rev")) + 1,
        "updated_by": me,
        "holder": me,
        "expires_at": now + TTL,
    })
    _log.info("sync: claimed compaction lease as %s", me)
    return True


def renew() -> bool:
    """Extend our own lease. Returns False if we are not the holder."""
    from aiforge_core.memory.sync.identity import self_id

    me = self_id()
    rec = read()
    if rec.get("holder") != me:
        return False
    rec["expires_at"] = int(time.time()) + TTL
    rec["rev"] = merge.as_rev(rec.get("rev")) + 1
    rec["updated_by"] = me
    _write(rec)
    return True


def is_holder() -> bool:
    from aiforge_core.memory.sync.identity import self_id

    rec = read()
    return bool(rec) and not _expired(rec) and rec.get("holder") == self_id()


def holder() -> str:
    """The peer id holding a *live* lease, or "" if nobody does."""
    rec = read()
    return "" if not rec or _expired(rec) else str(rec.get("holder") or "")


def may_compact() -> bool:
    """Whether this node is allowed to run compaction/dedupe/distillation now.

    The single place the lease policy lives, so callers stay callers. Compaction
    is blocked only when there is somebody else to collide with: a lone machine
    (no approved peers — the default) is always free to compact, and so is a
    mesh whose lease is unheld or expired. The lease is not a mutex; losing this
    check costs duplicate LLM work, never correctness.
    """
    from aiforge_core.memory.sync import peers
    from aiforge_core.memory.sync.identity import self_id

    if not peers.approved():
        return True
    live = holder()
    return not live or live == self_id()


def _heartbeat_tick() -> None:
    """Hold the lease if we have it, take it if it is going spare.

    Re-claiming is what recovers a dead leader's lease: whoever is still running
    picks it up once it expires. Skipped entirely with no approved peers so a
    single-machine install never writes a lease record it has no use for.
    """
    from aiforge_core.memory.sync import peers

    if not peers.approved():
        return
    if is_holder():
        renew()
    else:
        claim()


def _spawn(run) -> threading.Thread:
    """The one place a thread is created, so tests can take it away."""
    t = threading.Thread(target=run, name="aiforge-lease", daemon=True)
    t.start()
    return t


def start_heartbeat() -> None:
    """Keep the lease alive on its OWN timer, independent of the sync cycle.

    The sync cycle is half an hour and the lease lives ten minutes, so renewing
    once per cycle would let it lapse every time. The heartbeat therefore runs
    at ``RENEW_EVERY`` (3 min) in a daemon thread — three renewals per TTL, and
    it dies with the process. The first tick runs on the caller's thread so the
    lease is claimed before any work depending on it starts.
    """
    global _heartbeat

    _tick_safely()
    if _heartbeat is not None and _heartbeat.is_alive():
        return

    def _run() -> None:
        while True:
            time.sleep(RENEW_EVERY)
            _tick_safely()

    _heartbeat = _spawn(_run)


def _tick_safely() -> None:
    try:
        _heartbeat_tick()
    except Exception as exc:  # noqa: BLE001 — a lease we cannot write is not fatal
        _log.warning("sync: lease heartbeat failed: %s", exc)


__all__ = ["claim", "renew", "is_holder", "holder", "may_compact", "read",
           "start_heartbeat", "TTL", "RENEW_EVERY", "LEASE_KEY"]
