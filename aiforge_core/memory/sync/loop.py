"""One sync cycle, and the scheduler that repeats it.

Pull only, never push. A peer that is down is a request that returns nothing
this cycle; nothing blocks on it and nothing is queued for it. Every node
pulling from every other node is sufficient for the whole mesh to converge.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger("aiforge.sync")

DEFAULT_INTERVAL = 900  # 15 minutes


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


def run_once() -> list[dict]:
    """One cycle across every approved peer."""
    from aiforge_core.memory.sync import peers

    out = []
    for peer in peers.approved():
        try:
            out.append({"peer": peer.get("id"), **sync_with(peer)})
        except Exception as exc:  # noqa: BLE001 — one bad peer must not stop the rest
            _log.warning("sync: cycle failed for %s: %s", peer.get("id"), exc)
    return out


def run_forever(interval: int = DEFAULT_INTERVAL) -> None:
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
