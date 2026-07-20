"""One sync cycle, and the scheduler that repeats it.

Pull only, never push. A peer that is down is a request that returns nothing
this cycle; nothing blocks on it and nothing is queued for it. Every node
pulling from every other node is sufficient for the whole mesh to converge.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger("aiforge.sync")

DEFAULT_INTERVAL = 1800  # 30 minutes

# How long one cycle may spend on peers before it stops starting new ones.
# Per-request deadlines bound a single peer (``transport.REQUEST_DEADLINE``);
# they do not bound their sum, and MAX_PEERS is 64, each costing a manifest plus
# N blobs. A handful of dead or dribbling peers could therefore push one cycle
# past DEFAULT_INTERVAL, and since this daemon is strictly sequential that also
# stops the compaction pass that runs after it. A third of the interval leaves
# two thirds for compaction and the sleep; the skipped peers are simply pulled
# on the next cycle, which is what an unreachable peer costs anyway.
CYCLE_BUDGET = DEFAULT_INTERVAL // 3


def _first_url(peer: dict) -> str:
    urls = [u for u in (peer.get("urls") or []) if u]
    return urls[0] if urls else ""


def _peer_id(peer) -> str:
    """The id of a registry row that may be anything at all.

    peers.json is hand-editable and is also fed by gossip, so a row can be a
    string, ``null``, or a nested list. Reading an id must never be the thing
    that raises — see ``run_once``.
    """
    return str(peer.get("id") or "") if isinstance(peer, dict) else ""


def _ingest(entries) -> list[dict]:
    """Normalise a peer's manifest into the form the merge compares in.

    Only the hash needs it: the local side is a ``hexdigest`` and is therefore
    lowercase, so a peer emitting uppercase hex matches nothing we hold. Every
    round it produces the same unresolvable conflict, and the entry can never be
    applied — the two spellings never become equal on their own.
    """
    out: list[dict] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue          # a peer may send anything; a non-record is not one
        row = dict(e)
        row["hash"] = str(row.get("hash") or "").strip().lower()
        out.append(row)
    return out


def sync_with(peer: dict) -> dict:
    """Run one cycle against a single peer.

    Returns ``{ok, applied, rejected, conflicts}``. Never raises, and the counts
    survive a failure part-way through: under a global ENOSPC the per-entry
    applies were correctly counted as rejected and then the bookkeeping below
    raised, so the whole row was discarded and one WARNING was the only trace
    the peer had been tried at all.
    """
    result = {"ok": False, "applied": 0, "rejected": 0, "conflicts": 0}
    try:
        _pull(peer, result)
    except Exception as exc:  # noqa: BLE001 — a misbehaving peer is not our death
        _log.warning("sync: peer %s failed mid-cycle: %s", _peer_id(peer), exc)
    return result


def _pull(peer: dict, result: dict) -> None:
    """One peer's manifest, blobs and bookkeeping, accumulated into ``result``."""
    from aiforge_core.memory.sync import apply, manifest, merge, peers, transport

    base = _first_url(peer)
    if not base:
        return

    remote = transport.fetch_manifest(base, str(peer.get("token") or ""))
    if not remote:
        return
    result["ok"] = True

    token = str(peer.get("token") or "")
    local = manifest.build()
    plan = merge.plan_sync(local, _ingest(remote.get("manifest") or []))

    # A conflicting remote entry that also appears in `want` is the winner, so
    # the local copy is what is about to be lost; otherwise the local copy stays
    # and the remote's text is the version nothing else preserves.
    winning = {str(e.get("hash") or "") for e in plan["want"]}
    for pair in plan["conflict"]:
        losing_body = None
        if str(pair["remote"].get("hash") or "") not in winning:
            losing_body = transport.fetch_blob(base, str(pair["remote"].get("hash") or ""),
                                               token)
            if losing_body is None:
                continue      # nothing fetched, nothing to preserve
        if apply.keep_conflict(pair["local"], losing_body):
            result["conflicts"] += 1

    for entry in plan["want"]:
        body = transport.fetch_blob(base, str(entry.get("hash") or ""), token)
        if body is None:
            result["rejected"] += 1
            continue
        try:
            applied = apply.apply_blob(entry, body)
        except OSError as exc:
            # One unwritable record — a 400-character key is "File name too
            # long" — used to abort the cycle: every later entry was discarded
            # and `peers.touch` never ran, so the peer's last_seen froze, it
            # dropped out of the election, and the same entry killed the next
            # cycle too. It is re-advertised forever, so this must be per-entry.
            _log.warning("sync: could not apply %s: %s", entry.get("path"), exc)
            applied = False
        result["applied" if applied else "rejected"] += 1

    # ``roster`` is already coerced to a list by transport, so a peer on another
    # build cannot make this the line that ends the cycle.
    peers.merge_roster(remote.get("roster") or [])
    peers.touch(_peer_id(peer))

    _log.info("sync: %s applied=%d rejected=%d conflicts=%d", _peer_id(peer),
              result["applied"], result["rejected"], result["conflicts"])


def _ssdp_sweep() -> None:
    """Fold any locally-announced peers into the registry as candidates.

    Off unless ``AIFORGE_SYNC_SSDP=1``: multicast is useless across WireGuard
    and the internet, so it is opt-in for operators who genuinely have peers
    on the same physical segment. Discovered peers are folded through
    ``peers.merge_roster`` exactly like gossiped ones — this function does not
    (and must not) decide trust; a wildcard bind host is refused by
    ``discover`` itself, not re-checked here, so that guard lives in one place.
    """
    import os

    if os.environ.get("AIFORGE_SYNC_SSDP", "0") != "1":
        return
    from aiforge_core.memory.sync import discovery_ssdp, peers

    bind = os.environ.get("AIFORGE_SYNC_SSDP_HOST", "")
    try:
        found = discovery_ssdp.discover(bind)
    except ValueError as exc:
        # discover() refuses a wildcard/empty bind (DDoS-amplification guard).
        # That means SSDP is enabled without AIFORGE_SYNC_SSDP_HOST set to a
        # real interface address — a misconfiguration the operator needs to
        # see, not an ordinary "no peers on this segment" result, so it is
        # logged at a distinct level rather than folded into the info-level
        # best-effort case below.
        _log.warning("sync: ssdp enabled but misconfigured: %s "
                      "(set AIFORGE_SYNC_SSDP_HOST to a real interface address)", exc)
        return
    except Exception as exc:  # noqa: BLE001 — discovery is best-effort by nature
        _log.info("sync: ssdp sweep failed: %s", exc)
        return
    if found:
        peers.merge_roster(found)


def _skipped(peer) -> dict:
    """A peer the cycle ran out of budget for — reported, not silently absent."""
    return {"peer": _peer_id(peer), "ok": False, "applied": 0, "rejected": 0,
            "conflicts": 0, "skipped": True}


def run_once() -> list[dict]:
    """One cycle across every approved peer. Never raises.

    Reading the registry used to be outside every ``try``: a hand-edited or
    half-written peers.json (``{"peers": "beta"}``) raised out of here, out of
    ``run_forever``, and killed the process — which the supervisor restarted
    into the same file thirty seconds later, forever. Compaction rides this same
    loop, so it stopped too, from a cause no log connected to a peer file.
    """
    out: list[dict] = []
    try:
        from aiforge_core.memory.sync import peers

        _ssdp_sweep()
        roster = list(peers.approved())
    except Exception as exc:  # noqa: BLE001 — bad state is data, not a crash
        _log.warning("sync: cycle could not read the peer registry: %s", exc)
        return out

    started = time.monotonic()
    over_budget = False
    for peer in roster:
        try:
            if over_budget:
                out.append(_skipped(peer))
                continue
            row = {"peer": _peer_id(peer)}
            try:
                row.update(sync_with(peer))
            finally:
                # Appended whatever happened: a row with partial counts is the
                # only evidence on the admin page that the peer was tried.
                out.append(row)
            if time.monotonic() - started >= CYCLE_BUDGET:
                over_budget = True
                _log.warning("sync: cycle budget of %ds spent on/by peer %s — "
                             "skipping the rest of this cycle", CYCLE_BUDGET,
                             _peer_id(peer))
        except Exception as exc:  # noqa: BLE001 — one bad peer must not stop the rest
            _log.warning("sync: cycle failed for %s: %s", _peer_id(peer), exc)
    return out


def _start_ssdp_responder() -> None:
    """Answer other peers' searches for as long as this process runs.

    Same gate and same bind address as ``_ssdp_sweep``; started only from
    ``run_forever`` because ``run_once`` is a single-shot cycle and must not
    leave a thread behind. The SSDP message itself is built in
    ``discovery_ssdp`` — this only supplies who we are and where to reach us.
    """
    import os

    if os.environ.get("AIFORGE_SYNC_SSDP", "0") != "1":
        return
    from aiforge_core.memory.sync import discovery_ssdp, identity, peers

    bind = os.environ.get("AIFORGE_SYNC_SSDP_HOST", "")
    me = peers.load()["self"]
    peer_id = str(me.get("id") or "").strip() or identity.self_id()
    urls = [u for u in (me.get("urls") or []) if u]
    if not urls:
        _log.info("sync: ssdp responder not started: no self url in peers.json")
        return
    try:
        discovery_ssdp.serve_in_background(bind, peer_id, str(urls[0]))
    except Exception as exc:  # noqa: BLE001 — discovery is best-effort by nature
        _log.info("sync: ssdp responder failed to start: %s", exc)


def run_forever(interval: int = DEFAULT_INTERVAL) -> None:
    """Sync every ``interval`` seconds for as long as this process lives.

    Nothing here drives leadership: the compaction leader is *elected* from the
    peer registry each time somebody asks (``election.py``), so there is no
    record to claim and no heartbeat to keep alive.

    Knowledge compaction rides this cycle rather than owning a schedule of its
    own — one moving part. It runs *after* the sync pass so a cycle folds the
    data that cycle just pulled, and it can never take the daemon down: both
    tiers skip when their inputs are unchanged and soft-fail when they are not.

    Only a ``BaseException`` — a signal, a ``SystemExit`` — ends this loop. Any
    ordinary ``Exception`` from a cycle is logged and the next cycle runs.
    """
    from aiforge_core.memory.okf import tiers

    _start_ssdp_responder()
    while True:
        try:
            run_once()
            tiers.run_after_sync()
        except Exception as exc:  # noqa: BLE001 — outliving a bad cycle is the point
            # The daemon surviving one bad cycle is the entire reason this loop
            # exists: whatever broke (disk full, a state file somebody edited,
            # a peer nobody anticipated) is almost always transient or fixable
            # while we keep running, whereas exiting means the supervisor
            # restarts us straight back into it and compaction never runs again.
            _log.error("sync: cycle failed, continuing: %s", exc, exc_info=True)
        time.sleep(interval)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="AIForge peer memory sync")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.once:
        for row in run_once():
            print(row)
        return
    run_forever(args.interval)


__all__ = ["sync_with", "run_once", "run_forever", "main"]


if __name__ == "__main__":  # pragma: no cover — exercised by test_cli_entry
    # Without this, `python -m aiforge_core.memory.sync.loop` imports the module
    # and exits silently: the console script reaches main() but -m does not.
    main()
