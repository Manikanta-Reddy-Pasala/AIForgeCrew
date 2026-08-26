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


def _permitted(entries: list[dict], root) -> tuple[list[dict], dict]:
    """The entries the outbound filter allows, and a count per rule.

    Run HERE, at the offer, rather than at the send: an entry that never enters
    the offer is one the admin never learns exists. "We chose not to send it"
    and "we told them about it and then declined" are different guarantees, and
    only the first one is worth having.

    A node that cannot be read is held back rather than sent — the filter has to
    see the text to vouch for it, and an unreadable file is offered again next
    cycle once it can be read.
    """
    from aiforge_core.memory.okf import nodes
    from aiforge_core.memory.sync import redact, status

    kept: list[dict] = []
    blocked: dict[str, int] = {}
    for entry in entries:
        if entry.get("tomb"):
            # A tombstone is a deletion, not knowledge: it is JSON, it carries
            # no text that could leak, and filtering it means the deletion never
            # reaches the admin — the node stays in the fold forever and the
            # next pull bounces it back to the machine that deleted it.
            kept.append(entry)
            continue
        path = root / str(entry.get("path") or "")
        try:
            node = nodes.parse_node(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):   # UnicodeDecodeError is a ValueError
            blocked["filter.unreadable"] = blocked.get("filter.unreadable", 0) + 1
            continue
        verdict = redact.review(node)
        if verdict.send:
            kept.append(entry)
            continue
        blocked[verdict.rule] = blocked.get(verdict.rule, 0) + 1
        status.record_block(str(entry.get("key") or entry.get("path") or ""),
                            verdict.rule, verdict.reason)
    if blocked:
        _log.info("sync: filter held back %d node(s): %s",
                  sum(blocked.values()), blocked)
    return kept, blocked


# The bytes could not be produced. REJECT — the file is gone or unreadable, so
# it owes the counter a rejection. SKIP — it was edited inside the round trip;
# the next cycle offers the new bytes under their own hash, so it is neither a
# send nor a failure (exactly the original's uncounted `continue`).
_REJECT, _SKIP = object(), object()


def _read_verified_body(entry: dict, root, _io):
    """The bytes to push for one offered entry, or the ``_REJECT`` / ``_SKIP``
    sentinel when there are none.

    Re-read rather than cached: the manifest was built before the offer round
    trip, and a file edited inside that window must not be sent under its old
    hash — the admin verifies the two against each other and would refuse it
    every cycle.
    """
    path = root / str(entry.get("path") or "")
    try:
        body = path.read_bytes() if _io.is_syncable(path) else None
    except OSError:
        body = None
    if body is None:
        return _REJECT
    if _io.sha256_bytes(body) != str(entry.get("hash") or "").lower():
        _log.info("sync: %s changed during the cycle — offering it again "
                  "next time", entry.get("path"))
        return _SKIP
    return body


def _push_wanted(base_url: str, want: list, result: dict, deadline,
                 root, _io, transport, group: str = "") -> None:
    """Push each wanted entry, honouring the cycle budget. The budget stops the
    loop part-way and the rest are re-offered next cycle."""
    import time
    for entry in want:
        if deadline is not None and time.monotonic() >= deadline:
            _log.warning("sync: cycle budget spent part-way through the push "
                         "— %d entry(ies) left for the next cycle",
                         len(want) - result["pushed"] - result["rejected"])
            return
        body = _read_verified_body(entry, root, _io)
        if body is _SKIP:
            continue
        if body is _REJECT:
            result["rejected"] += 1
            continue
        ok = transport.push_blob(base_url, entry, body, group=group)
        result["pushed" if ok else "rejected"] += 1


def run_once(base_url: str, deadline: float | None = None, *,
             group: str = "") -> dict:
    """Offer and push one cycle's worth. Never raises.

    ``deadline`` is a ``time.monotonic`` stamp after which no further entry is
    sent — the same budget the pull half honours. Remaining entries are simply
    re-offered next cycle; the offer is computed fresh each time, so nothing has
    to be remembered between cycles.
    """
    from aiforge_core.memory.sync import _io, manifest, transport

    result = {"ok": False, "offered": 0, "pushed": 0, "rejected": 0,
              "blocked": 0, "blocked_by_rule": {}, "pending": 0}
    try:
        entries, blocked = _permitted(_mine(manifest.build()), _io.root())
        result["offered"] = len(entries)
        result["blocked"] = sum(blocked.values())
        result["blocked_by_rule"] = blocked
        want = transport.offer(base_url, entries, group=group)
        if want is None:
            return result          # admin unreachable: nothing sent, no error
        result["ok"] = True
        # What the admin asked for and has not acknowledged yet. Recomputed from
        # the tree every cycle rather than queued, so a successful push makes it
        # fall to zero by construction — there is no outbox to drift or clear.
        result["pending"] = len(want)
        _push_wanted(base_url, want, result, deadline, _io.root(), _io,
                     transport, group)
    except Exception as exc:  # noqa: BLE001 — an unreachable admin is not our death
        _log.warning("sync: push to %s failed mid-cycle: %s", base_url, exc)
    _log.info("sync: push offered=%d pushed=%d rejected=%d blocked=%d",
              result["offered"], result["pushed"], result["rejected"],
              result["blocked"])
    return result


__all__ = ["run_once"]
