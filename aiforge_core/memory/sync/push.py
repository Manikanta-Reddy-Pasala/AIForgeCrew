"""The spoke side of a cycle: offer what we authored, send what is wanted.

Two round trips, and the admin decides both times:

1. **offer** — we POST our manifest and the admin answers with the entries it
   does not already hold. Nothing is sent blind, so a spoke that has been
   offline for a month costs one small request rather than a re-upload of its
   whole tree.
2. **push** — one request per wanted entry, each carrying the entry and its
   bytes. Per-entry so a single unwritable record (a key no filesystem can
   hold) costs itself and not the rest of the batch.

Push, not pull-from-the-admin: only the admin needs a reachable address. A
laptop on a hotel network, a machine behind NAT and a box on the office LAN are
the same case — they open the connection, so nothing has to be routable to them.

What travels up is the knowledge this machine authored: class B nodes and
tombstones minted here. Raw captures and locally-compacted briefs stay put —
every machine runs its own compaction, so its briefs are its own business, and
its captures are the input to that. What comes back down is the admin's merge,
pulled separately by ``loop._pull``. A spoke never sees another spoke's raw
nodes, which is the whole point of the hub — the admin merges them first.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("aiforge.sync")


def _mine(entries: list[dict]) -> list[dict]:
    """The entries this machine may push: class B nodes and tombstones we minted.

    Class A — ``captures/`` and ``compacted/`` — is deliberately excluded. Those
    are one machine's raw text and the briefs it distilled from that text
    locally; sending them would duplicate compaction that has already happened
    and push everyone's raw pastes across the network for nothing.

    Anything ``derived`` is excluded for a different reason: a spoke holds the
    admin's merge in ``mesh/`` (it pulled it), and offering that back would make
    every cycle a wall of "refusing derived node" on the admin.
    """
    from aiforge_core.memory.sync import identity, paths

    me = paths.fold(identity.self_id())
    return [e for e in entries
            if e.get("kind") == "B"
            and not str(e.get("derived") or "").strip()
            and paths.fold(str(e.get("origin") or "")) == me]


def run_once(base_url: str, deadline: float | None = None) -> dict:
    """Offer and push one cycle's worth. Never raises.

    ``deadline`` is a ``time.monotonic`` stamp after which no further entry is
    sent — the same budget the pull half honours. Remaining entries are simply
    re-offered next cycle; the offer is computed fresh each time, so nothing has
    to be remembered between cycles.
    """
    import time

    from aiforge_core.memory.sync import _io, manifest, transport

    result = {"ok": False, "offered": 0, "pushed": 0, "rejected": 0}
    try:
        entries = _mine(manifest.build())
        result["offered"] = len(entries)
        want = transport.offer(base_url, entries)
        if want is None:
            return result          # admin unreachable: nothing sent, no error
        result["ok"] = True
        root = _io.root()
        for entry in want:
            if deadline is not None and time.monotonic() >= deadline:
                _log.warning("sync: cycle budget spent part-way through the push "
                             "— %d entry(ies) left for the next cycle",
                             len(want) - result["pushed"] - result["rejected"])
                break
            path = root / str(entry.get("path") or "")
            try:
                # Re-read rather than cached: the manifest was built before the
                # offer round trip, and a file edited inside that window must
                # not be sent under its old hash — the admin verifies the two
                # against each other and would refuse it every cycle.
                body = path.read_bytes() if _io.is_syncable(path) else None
            except OSError:
                body = None
            if body is None:
                result["rejected"] += 1
                continue
            if _io.sha256_bytes(body) != str(entry.get("hash") or "").lower():
                # Edited inside the round trip. Sending it would fail the
                # admin's hash check; the next cycle offers the new bytes under
                # their own hash, so this is a skip rather than a failure.
                _log.info("sync: %s changed during the cycle — offering it again "
                          "next time", entry.get("path"))
                continue
            ok = transport.push_blob(base_url, entry, body)
            result["pushed" if ok else "rejected"] += 1
    except Exception as exc:  # noqa: BLE001 — an unreachable admin is not our death
        _log.warning("sync: push to %s failed mid-cycle: %s", base_url, exc)
    _log.info("sync: push offered=%d pushed=%d rejected=%d", result["offered"],
              result["pushed"], result["rejected"])
    return result


__all__ = ["run_once"]
