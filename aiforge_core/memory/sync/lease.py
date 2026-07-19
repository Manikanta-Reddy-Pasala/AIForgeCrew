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
import time

from aiforge_core.memory.sync import _io, merge, paths
from aiforge_core.memory.sync.paths import LEASE_KEY

_log = logging.getLogger("aiforge.sync")

TTL = 600          # 10 minutes
RENEW_EVERY = 180  # 3 minutes


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


__all__ = ["claim", "renew", "is_holder", "read", "TTL", "RENEW_EVERY", "LEASE_KEY"]
