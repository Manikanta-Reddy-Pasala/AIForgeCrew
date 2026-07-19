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


def delete_node(origin: str, key: str) -> bool:
    """Remove a node and record a tombstone. False if no such identity exists."""
    from aiforge_core.memory.okf import nodes as _nodes
    from aiforge_core.memory.sync.identity import self_id

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

    _io.write_json(paths.tomb_path(origin, key),
                   {"origin": origin, "key": key, "rev": rev + 1,
                    "updated_by": self_id(), "tomb": True})
    _log.info("sync: tombstoned (%s, %s) at rev %d", origin, key, rev + 1)
    return True


__all__ = ["delete_node"]
