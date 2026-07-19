"""Merge rules for the sync protocol. Pure — no filesystem, no network.

Class A records are immutable and content-addressed, so union by hash converges
without coordination. Class B records are identified by ``(origin, key)`` and
ordered by ``(rev, updated_by, hash)``: a higher revision wins, an equal
revision breaks on the writer's slug, and a still-equal writer breaks on the
content hash. That last term is what makes the order *total* — without it two
peers holding the same rev and writer but different bytes each conclude the
other is not newer, neither fetches, and the conflict is reported forever.

An equal revision with differing content means two peers edited the same node
before either synced. The winner is still deterministic, but the collision is
reported so the caller can preserve the losing text rather than discard it.

``as_rev`` lives here rather than beside the disk code because this module
imports nothing from the package: the manifest can reuse it without a cycle,
and one coercion rule serves both sides of the protocol.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("aiforge.sync")


def as_rev(value) -> int:
    """Coerce an untrusted ``rev`` to an int, defaulting to 0.

    ``rev`` reaches us from hand-edited frontmatter and from peers running other
    versions, so ``"v2"`` is a thing that happens. A bare ``int()`` here would
    abort the whole merge — losing every well-formed entry beside the bad one —
    to punish a single malformed record.
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):  # noqa: BLE001 — one bad record, not a dead mesh
        return 0


def _ident(entry: dict) -> tuple[str, str]:
    return (str(entry.get("origin") or ""), str(entry.get("key") or ""))


def _order(entry: dict) -> tuple[int, str, str]:
    """Total order over one identity's versions. See the module docstring."""
    return (as_rev(entry.get("rev")), str(entry.get("updated_by") or ""),
            str(entry.get("hash") or ""))


def plan_sync(local: list[dict], remote: list[dict]) -> dict:
    """Decide what to fetch from a peer.

    Returns ``{"want": [remote entries to fetch], "conflict": [{local, remote}]}``.
    A conflicting entry may also appear in ``want`` when the remote is the winner.
    """
    have = {h for e in local if e.get("cls") == "A" and (h := e.get("hash"))}

    # One identity can appear twice locally (the same node held in two scopes).
    # The highest version is the one compared, matching the file
    # ``paths.node_paths`` would write to — see I1.
    by_ident: dict[tuple[str, str], dict] = {}
    for e in local:
        if e.get("cls") != "B":
            continue
        ident = _ident(e)
        cur = by_ident.get(ident)
        if cur is None or _order(e) > _order(cur):
            by_ident[ident] = e

    want: list[dict] = []
    conflict: list[dict] = []

    for r in remote:
        if not r.get("hash"):
            # Without a hash we can neither tell it apart from what we hold nor
            # verify the blob on arrival. Treating it as already-present (the
            # old behaviour, via None landing in `have`) silently dropped it.
            _log.warning("sync: peer entry %s has no hash, skipping", r.get("path"))
            continue

        if r.get("cls") == "A":
            if r["hash"] not in have:
                want.append(r)
            continue

        cur = by_ident.get(_ident(r))
        if cur is None:
            want.append(r)
            continue
        if cur.get("hash") == r.get("hash"):
            continue

        if as_rev(cur.get("rev")) == as_rev(r.get("rev")):
            conflict.append({"local": cur, "remote": r})
        if _order(r) > _order(cur):
            want.append(r)

    return {"want": want, "conflict": conflict}


__all__ = ["as_rev", "plan_sync"]
