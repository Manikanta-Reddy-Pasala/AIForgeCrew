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

# How many consecutive cycles a *reachable but non-folding* leader may leave the
# mesh un-distilled before the next candidate takes over. The election proves a
# peer answers /manifest; it proves nothing about whether that peer runs the
# fold — the API app and the sync loop are separate entry points, so a peer
# running only ``aiforge-api`` is a perfect passive leader (it serves manifests,
# never distils), and every follower deferring to it keeps an empty view
# forever. This is the escape hatch, deliberately NOT the default: the single-
# leader design (one fold, no duplicate LLM work) is what runs when the leader is
# healthy. Three cycles of grace so the leader's first fold has time to sync out
# before anyone pre-empts it — the same slack ALIVE_WINDOW gives reachability.
FALLBACK_AFTER = 3

# Local bookkeeping for the fallback above, in a dotfile at the memory-tree root.
# The election *result* is still stateless — computed from replicated data, no
# record claimed or renewed (see the module docstring). Only the "has the elected
# leader been silent long enough to pre-empt?" timer is local, and it must be:
# there is no replicated fact that says "the leader is reachable but idle", and a
# wall-clock stamp would drag a foreign clock back into a calculation built to
# avoid one. No manifest scan reaches a root dotfile, so it never travels.
_STATE_FILE = ".election.json"


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


def _read_state() -> dict:
    from aiforge_core.memory.sync import _io

    try:
        return _io.read_json(_io.root() / _STATE_FILE)
    except Exception as exc:  # noqa: BLE001 — a lost timer costs a cycle, never work
        _log.info("sync: could not read the fallback state (%s)", exc)
        return {}


def _write_state(state: dict) -> None:
    from aiforge_core.memory.sync import _io

    try:
        _io.write_json(_io.root() / _STATE_FILE, state)
    except OSError as exc:  # a dropped timer just delays the fallback by a cycle
        _log.info("sync: could not record the fallback state (%s)", exc)


def _observe_leader(elected: str, me: str) -> None:
    """Advance — or clear — the passive-leader timer. Exactly once per cycle.

    Called from :func:`may_distil`, which ``tiers.distil_mesh`` invokes once each
    cycle before it folds. Kept out of :func:`effective_leader` on purpose: that
    one is a pure read, called again by the view tier in the same cycle, and must
    not double-count.

    The signal is whether the elected leader's fold is *visible here* — a mesh
    node under ``mesh/<leader>/``. A healthy leader produces one and it syncs
    out, so the timer resets and we defer as before; a leader that only serves
    the API never does, and the count climbs until the fallback fires.
    """
    if elected == me:
        # We lead: nothing to time, and a stale count left over from a past
        # demotion must never make us pre-empt our own fold.
        if _read_state():
            _write_state({})
        return

    from aiforge_core.memory.okf import tiers

    try:
        producing = tiers.leader_has_mesh_output(elected)
    except Exception as exc:  # noqa: BLE001 — an unreadable mesh is not "idle"
        _log.info("sync: cannot read the leader's fold (%s) — assuming it is live", exc)
        producing = True

    state = _read_state()
    if producing:
        if state:
            _write_state({})
        return
    count = int(state.get("misses") or 0) + 1 if state.get("leader") == elected else 1
    # Clamped at the threshold: once passive it stays passive, and a count that
    # never grows means the write is a no-op we can skip — a mesh that keeps a
    # steady passive leader must not rewrite this file every cycle forever.
    count = min(count, FALLBACK_AFTER)
    if state.get("leader") != elected or int(state.get("misses") or 0) != count:
        _write_state({"leader": elected, "misses": count})


def _leader_is_passive(elected: str) -> bool:
    """Pure read of the timer :func:`_observe_leader` maintains: the elected
    leader has gone ``FALLBACK_AFTER`` cycles without a fold visible here."""
    state = _read_state()
    return (state.get("leader") == elected
            and int(state.get("misses") or 0) >= FALLBACK_AFTER)


def effective_leader() -> str:
    """The peer this node treats as leader for folding *and* for view trust.

    Normally the elected leader. But the election only proves reachability, so a
    leader that answers /manifest yet never runs the fold would starve the mesh
    forever. Once that leader has been provably silent for ``FALLBACK_AFTER``
    cycles, the next live candidate takes over. Deterministic — every follower
    drops the same passive leader and promotes the same successor — so the mesh
    still has exactly ONE folder, never an N-way duplicate fold.

    Read-only. The timer is advanced only by :func:`_observe_leader`, so the fold
    tier and the view tier compute the same effective leader within a cycle.
    """
    elected = leader()
    if not _leader_is_passive(elected):
        return elected
    for candidate in candidates():
        if candidate != elected:
            return candidate
    return elected


def may_distil() -> bool:
    """Whether this node may run the LLM-expensive, non-deterministic work.

    The single place the "am I the one?" policy lives, so callers stay callers.
    Soft-fails OPEN: losing distillation entirely because a registry file is
    unreadable is far worse than the duplicate work a wrong answer causes —
    which this design tolerates by construction.

    True for the elected leader as before, and *additionally* for the next
    candidate once the elected leader is proven passive (reachable but not
    folding). Without that second clause a mesh whose leader runs only the API
    stays un-distilled and every view stays empty, with every log line reading
    ``ok: True`` — the failure has no other symptom.
    """
    try:
        if is_leader():
            return True
    except Exception as exc:  # noqa: BLE001 — never let the election block work
        _log.info("sync: election failed (%s) — proceeding as leader", exc)
        return True
    # We are not the *elected* leader, and the election computed cleanly. Time
    # the leader and, once it is proven passive, fall back if we are next in
    # line. Guarded the same soft-fail-OPEN way: a fallback check that raises
    # must not be the thing that costs the mesh its distillation.
    try:
        elected, me = leader(), _me()
        _observe_leader(elected, me)          # the one per-cycle timer update
        if effective_leader() == me:
            _log.warning("sync: elected leader %s has not distilled in %d cycles "
                         "— folding locally as the next candidate", elected,
                         FALLBACK_AFTER)
            return True
    except Exception as exc:  # noqa: BLE001 — a broken fallback check fails OPEN too
        _log.info("sync: fallback election failed (%s) — proceeding as leader", exc)
        return True
    _log.info("sync: deferring distillation to elected leader %s", elected)
    return False


__all__ = ["ALIVE_WINDOW", "FALLBACK_AFTER", "candidates", "leader", "is_leader",
           "leader_name", "effective_leader", "may_distil"]
