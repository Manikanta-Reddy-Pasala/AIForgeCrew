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


def _first_url(peer: dict) -> str:
    urls = [u for u in (peer.get("urls") or []) if u]
    return urls[0] if urls else ""


def sync_with(peer: dict) -> dict:
    """Run one cycle against a single peer.

    Returns ``{ok, applied, rejected, conflicts}``. Never raises: an unreachable
    or misbehaving peer must not take the local node down.
    """
    from aiforge_core.memory.sync import apply, manifest, merge, peers, transport

    result = {"ok": False, "applied": 0, "rejected": 0, "conflicts": 0}
    base = _first_url(peer)
    if not base:
        return result

    remote = transport.fetch_manifest(base, str(peer.get("token") or ""))
    if not remote:
        return result
    result["ok"] = True

    local = manifest.build()
    plan = merge.plan_sync(local, remote.get("manifest") or [])

    for pair in plan["conflict"]:
        if apply.keep_conflict(pair["local"]):
            result["conflicts"] += 1

    for entry in plan["want"]:
        body = transport.fetch_blob(base, str(entry.get("hash") or ""),
                                    str(peer.get("token") or ""))
        if body is None:
            result["rejected"] += 1
            continue
        if apply.apply_blob(entry, body):
            result["applied"] += 1
        else:
            result["rejected"] += 1

    peers.merge_roster(remote.get("roster") or [])
    peers.touch(str(peer.get("id") or ""))

    _log.info("sync: %s applied=%d rejected=%d conflicts=%d", peer.get("id"),
              result["applied"], result["rejected"], result["conflicts"])
    return result


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


def run_once() -> list[dict]:
    """One cycle across every approved peer."""
    from aiforge_core.memory.sync import peers

    _ssdp_sweep()
    out = []
    for peer in peers.approved():
        try:
            out.append({"peer": peer.get("id"), **sync_with(peer)})
        except Exception as exc:  # noqa: BLE001 — one bad peer must not stop the rest
            _log.warning("sync: cycle failed for %s: %s", peer.get("id"), exc)
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

    The compaction lease is deliberately NOT driven from this loop: at a
    30-minute interval and a 10-minute TTL a per-cycle renew would always be
    too late. ``lease.start_heartbeat`` claims it here, on this thread, and then
    keeps it on its own ``RENEW_EVERY`` timer (see lease.py).
    """
    from aiforge_core.memory.sync import lease

    _start_ssdp_responder()
    lease.start_heartbeat()
    while True:
        run_once()
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
