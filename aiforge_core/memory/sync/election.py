"""Who distils? A deterministic election, computed from replicated data alone.

Replication needs no leader. Distillation does — compaction, OKF node
deduplication and summarisation are LLM-expensive and non-deterministic, so two
peers running them over the same input produce two different answers.

The rule is one line: **the lexicographically smallest live candidate leads**,
where the candidates are ourselves plus every approved peer we still consider
alive. Every peer computes it from data it already has, so no record has to be
claimed, renewed or replicated for an election to resolve.

Why not a lease. A wall-clock lease cannot elect anything across a 30-minute
pull cycle: peer B first sees A's lease record ~1800s old against a 600s TTL,
concludes it is free, claims it, and both peers compact — every cycle, forever.
Raising the TTL only moves the number; comparing one machine's clock against
another's over a slow channel is the wrong mechanism, so there is no lease.

The only clock read here is our own, twice: ``last_seen`` is stamped by
``peers.touch()`` when *we* successfully pulled from a peer, so ``now -
last_seen`` subtracts two readings of one clock. A peer's own clock never
enters the calculation, which is what makes the result skew-free.

Peers may briefly disagree — A can reach C while B cannot, so they elect
differently for a cycle or two. That is tolerated by design: duplicate briefs
are content-addressed and the next dedupe pass merges them. The cost of a split
election is wasted tokens, never corruption, which is exactly why this is not a
consensus protocol.
"""
from __future__ import annotations

import logging
import time

from aiforge_core.memory.sync import loop

_log = logging.getLogger("aiforge.sync")

# Three sync cycles. ``last_seen`` only advances when a pull SUCCEEDS, so a live
# peer is invisible between cycles by construction; at one cycle the window
# would expire exactly when the next pull is due and leadership would flap on
# every slow, retried or skipped cycle. Three gives two whole cycles of slack
# while still handing a genuinely dead leader over inside a couple of hours.
# Expressed in cycles, not seconds, so it tracks DEFAULT_INTERVAL if that moves.
ALIVE_WINDOW = 3 * loop.DEFAULT_INTERVAL


def _me() -> str:
    """Our own id in the form the roster is compared in."""
    from aiforge_core.memory.sync import identity, peers

    mine = identity.self_id()
    return peers.normalise_id(mine) or mine


def candidates() -> list[str]:
    """Every peer id eligible to lead, sorted. Always includes us.

    An approved peer we have never reached (``last_seen`` 0) is not a candidate:
    it may not exist. With no approved peers the list is just us, so a single
    machine always leads and behaves exactly as it did before the mesh existed.

    Ids are compared in canonical form (``peers.normalise_id``, the same slug
    ``identity.self_id`` produces) and ones that will not round-trip
    ``paths.is_addressable`` are dropped. A roster row typed by a human as
    ``NUC-Prod`` for a peer that calls itself ``nuc-prod`` otherwise elects a
    machine that does not exist — and since uppercase sorts first, it wins:
    every peer defers to it, nothing is ever distilled, and the only trace is
    one log line.
    """
    from aiforge_core.memory.sync import peers

    now = int(time.time())
    out = {_me()}
    for p in peers.approved():
        pid = peers.normalise_id(p.get("id"))
        seen = peers.as_epoch(p.get("last_seen"))
        # OUR clock on both sides of the subtraction (see module docstring) —
        # and both ends of the window. A stamp AHEAD of our clock (bad RTC, an
        # NTP step, a hand-edited file) makes the difference negative, which an
        # upper bound alone accepts forever: a peer stamped two years ahead
        # stays a live candidate for two years, and if its id sorts first
        # nothing ever distils.
        if pid and seen and 0 <= (now - seen) <= ALIVE_WINDOW:
            out.add(pid)
    return sorted(out)


def leader() -> str:
    """The elected leader's peer id."""
    return candidates()[0]


def is_leader() -> bool:
    return leader() == _me()


def leader_name() -> str:
    """:func:`leader` for display and result payloads — ``"?"`` if unreadable."""
    try:
        return leader()
    except Exception as exc:  # noqa: BLE001 — a label must never raise
        _log.info("sync: cannot name the leader (%s)", exc)
        return "?"


def may_distil() -> bool:
    """Whether this node may run the LLM-expensive, non-deterministic work.

    The single place the "am I the one?" policy lives, so callers stay callers.
    Soft-fails OPEN: losing distillation entirely because a registry file is
    unreadable is far worse than the duplicate work a wrong answer causes —
    which this design tolerates by construction.
    """
    try:
        if is_leader():
            return True
    except Exception as exc:  # noqa: BLE001 — never let the election block work
        _log.info("sync: election failed (%s) — proceeding as leader", exc)
        return True
    _log.info("sync: deferring distillation to elected leader %s", leader_name())
    return False


__all__ = ["ALIVE_WINDOW", "candidates", "leader", "is_leader", "leader_name",
           "may_distil"]
