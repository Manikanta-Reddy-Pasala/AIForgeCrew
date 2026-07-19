"""Merge rules for the sync protocol. Pure — no filesystem, no network.

Class A records are immutable and content-addressed, so union by hash converges
without coordination. Class B records are identified by ``(origin, key)`` and
ordered by ``(rev, updated_by)``: a higher revision wins, and an equal revision
breaks on the writer's slug so every peer independently reaches the same answer.

An equal revision with differing content means two peers edited the same node
before either synced. The winner is still deterministic, but the collision is
reported so the caller can preserve the losing text rather than discard it.
"""
from __future__ import annotations


def _ident(entry: dict) -> tuple[str, str]:
    return (str(entry.get("origin") or ""), str(entry.get("key") or ""))


def _order(entry: dict) -> tuple[int, str]:
    return (int(entry.get("rev") or 0), str(entry.get("updated_by") or ""))


def plan_sync(local: list[dict], remote: list[dict]) -> dict:
    """Decide what to fetch from a peer.

    Returns ``{"want": [remote entries to fetch], "conflict": [{local, remote}]}``.
    A conflicting entry may also appear in ``want`` when the remote is the winner.
    """
    have = {e.get("hash") for e in local if e.get("cls") == "A"}
    by_ident = {_ident(e): e for e in local if e.get("cls") == "B"}

    want: list[dict] = []
    conflict: list[dict] = []

    for r in remote:
        if r.get("cls") == "A":
            if r.get("hash") not in have:
                want.append(r)
            continue

        cur = by_ident.get(_ident(r))
        if cur is None:
            want.append(r)
            continue
        if cur.get("hash") == r.get("hash"):
            continue

        if int(cur.get("rev") or 0) == int(r.get("rev") or 0):
            conflict.append({"local": cur, "remote": r})
        if _order(r) > _order(cur):
            want.append(r)

    return {"want": want, "conflict": conflict}


__all__ = ["plan_sync"]
