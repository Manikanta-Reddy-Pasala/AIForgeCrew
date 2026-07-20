"""Deletion, expressed so the mesh can hear it.

A grow-only set cannot say "removed" — unlinking a file locally is undone by
the next pull. A tombstone is a class B record carrying the identity and a
revision one higher than the node it replaces, so it beats the version every
other peer is still holding, and a genuinely newer edit later beats it back.
"""
from __future__ import annotations

import logging

from aiforge_core.memory.sync import _io, merge, paths

_log = logging.getLogger("aiforge.sync")


def _record(origin: str, key: str, rev: int) -> None:
    """Write the tombstone itself, at one revision above what it replaces."""
    from aiforge_core.memory.sync.identity import self_id

    _io.write_json(paths.tomb_path(origin, key),
                   {"origin": origin, "key": key, "rev": rev + 1,
                    "updated_by": self_id(), "tomb": True})
    _log.info("sync: tombstoned (%s, %s) at rev %d", origin, key, rev + 1)


def delete_node(origin: str, key: str) -> bool:
    """Remove a node and record a tombstone. False if no such identity exists."""
    from aiforge_core.memory.okf import nodes as _nodes

    found = paths.node_paths(origin, key)
    if not found:
        return False

    rev = 0
    for p in found:
        try:
            meta = (_nodes.parse_node(p.read_text(encoding="utf-8")).get("meta") or {})
            rev = max(rev, merge.as_rev(meta.get("rev")))
        except Exception:  # noqa: BLE001 — an unreadable node is still deletable
            continue

    for p in found:
        p.unlink(missing_ok=True)

    _record(origin, key, rev)
    return True


def mark_deleted(origin: str, key: str, rev) -> bool:
    """Tombstone an identity whose file the caller has *already* removed.

    The producer half of deletion, for the two paths that remove a node their
    own way — ``okf.author`` moves it to ``okf/.trash/`` and ``store.dedupe_nodes``
    unlinks a duplicate — and so cannot use :func:`delete_node`, which does the
    removal itself. Without it the removal is local only: the next pull from any
    peer still holding the node re-plants it, forever.

    Two refusals, both silent no-ops rather than errors:

    * an ``origin`` that is not this machine. Only the minting peer may speak for
      its identity, and ``apply._accept_class_b`` already refuses an inbound
      entry claiming ours — minting one for somebody else would be that same
      forgery, outbound.
    * an identity that still has a file on disk. Ids are per-scope counters, so
      the same ``(origin, key)`` can legitimately name a node in ``global/`` and
      one in ``projects/<repo>/``; a tombstone here would delete the survivor on
      every peer.

    Soft-fails: a tombstone we could not write must not undo the removal the
    operator asked for, so the failure is logged and the caller carries on.
    """
    from aiforge_core.memory.sync.identity import self_id

    origin, key = str(origin or ""), str(key or "")
    if not origin or not key:
        return False
    if paths.fold(origin) != paths.fold(self_id()):
        _log.info("sync: not tombstoning (%s, %s) — another peer's identity",
                  origin, key)
        return False
    if paths.node_paths(origin, key):
        _log.info("sync: not tombstoning (%s, %s) — another copy of it survives",
                  origin, key)
        return False
    try:
        _record(origin, key, merge.as_rev(rev))
    except OSError as exc:  # the delete stands either way; only the mesh misses it
        _log.warning("sync: could not tombstone (%s, %s): %s", origin, key, exc)
        return False
    return True


__all__ = ["delete_node", "mark_deleted"]
