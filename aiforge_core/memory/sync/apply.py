"""Placing a fetched blob into the local tree. Knows nothing about HTTP.

Every blob is verified against the hash its peer advertised before it touches
the tree, and every write goes through ``_io.write_atomic``, so an interrupted
or tampered fetch can never leave a partial or forged note behind. A rejected
blob is simply dropped — it reappears in the next diff.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from aiforge_core.memory.sync import _io, paths

_log = logging.getLogger("aiforge.sync")


def apply_blob(entry: dict, body: bytes) -> bool:
    """Verify and write one fetched blob. False means it was rejected."""
    if hashlib.sha256(body).hexdigest() != str(entry.get("hash") or ""):
        _log.warning("sync: hash mismatch for %s, dropping", entry.get("path"))
        return False

    target = paths.target_for(entry)
    if target is None:
        return False

    _io.write_atomic(target, body)
    _enforce_invariant(entry)
    return True


def _enforce_invariant(entry: dict) -> None:
    """For one (origin, key), either the node file or its tombstone exists, never both."""
    if entry.get("kind") != "B":
        return
    key = str(entry.get("key") or "")
    if not key:
        return
    origin = str(entry.get("origin") or "")
    if entry.get("tomb"):
        for p in paths.node_paths(origin, key):
            p.unlink(missing_ok=True)
    else:
        paths.tomb_path(origin, key).unlink(missing_ok=True)


def keep_conflict(local_entry: dict) -> Path | None:
    """Preserve a losing local version beside the node as a ``.conflict`` sidecar.

    Sidecars are local artefacts, excluded from the manifest: replicating them
    would multiply one collision across the whole mesh.
    """
    target = _io.safe_target(str(local_entry.get("path") or ""))
    if target is None or not target.is_file():
        return None
    sidecar = target.with_name(target.stem + ".conflict.md")
    try:
        _io.write_atomic(sidecar, target.read_bytes())
    except OSError:  # losing the sidecar must not abort the sync
        _log.warning("sync: could not write conflict sidecar for %s", target)
        return None
    _log.info("sync: conflict on %s, kept losing version at %s",
              local_entry.get("key"), sidecar.name)
    return sidecar


__all__ = ["apply_blob", "keep_conflict"]
