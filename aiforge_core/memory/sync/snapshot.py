"""Revert points for a memory tree.

Cheap enough to take before every fold, because the tree is small markdown files
and a hardlink copy costs inodes rather than bytes. That affordability is the
whole design: a snapshot somebody has to remember to take is one nobody has.

Lives in ``.snapshots`` — DOTTED, deliberately. ``_io._hidden_below`` already
excludes a dotted directory below a scanned root from the manifest, so a
snapshot can never be advertised to a peer, served over ``/blob`` or re-planted
somewhere else. Renaming this constant to something undotted would silently
replicate every revert point to the whole fleet.

Hardlinks are safe here because every writer in this codebase writes atomically
(``_io.write_atomic`` stages a ``.tmp`` and renames): a later write REPLACES the
directory entry and never mutates the inode the snapshot is holding. A writer
that opened a file for in-place modification would break that, and would have to
be given its own copy-on-write path.

A revert **snapshots the current state before it restores**, so a revert is
itself revertible. An operator who reverts to the wrong stamp has made a
recoverable mistake rather than destroyed the state they meant to keep.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

_log = logging.getLogger("aiforge.sync")

DIR = ".snapshots"

# The subtrees worth a revert point: the received inbox and the fold. ``okf/``
# is authored by hand and is never written by sync (``_io.assert_not_ours``), so
# it is not this feature's to roll back — reverting it would destroy work that
# sync never touched in the first place.
SUBTREES = ("peers", "mesh")

_DEFAULT_KEEP = 10


def keep() -> int:
    """How many snapshots to retain. ``AIFORGE_SYNC_SNAPSHOTS`` overrides."""
    try:
        return max(1, int(os.environ.get("AIFORGE_SYNC_SNAPSHOTS") or _DEFAULT_KEEP))
    except ValueError:
        return _DEFAULT_KEEP


def _dir(root: Path) -> Path:
    return Path(root) / DIR


def _now_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime())


def take(root: Path, stamp: str = "") -> str:
    """Snapshot ``root``'s syncable subtrees. Returns the stamp. Never raises.

    A snapshot that cannot be taken must not stop the fold or the pull it
    precedes: that work is the product and the revert point is insurance, so a
    full disk costs a warning rather than a stalled fleet.
    """
    root = Path(root)
    stamp = stamp or _now_stamp()
    target = _dir(root) / stamp
    try:
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        for name in SUBTREES:
            src = root / name
            if src.is_dir():
                shutil.copytree(src, target / name, copy_function=os.link,
                                dirs_exist_ok=True)
        _prune(root)
    except OSError as exc:
        _log.warning("sync: could not snapshot %s: %s", root, exc)
    return stamp


def listing(root: Path) -> list[dict]:
    """Every snapshot, newest first. Sorted by stamp, which sorts as it reads."""
    d = _dir(Path(root))
    if not d.is_dir():
        return []
    rows = [{"stamp": p.name,
             "files": sum(1 for f in p.rglob("*") if f.is_file())}
            for p in d.iterdir() if p.is_dir()]
    return sorted(rows, key=lambda r: r["stamp"], reverse=True)


def _prune(root: Path) -> None:
    for row in listing(root)[keep():]:
        try:
            shutil.rmtree(_dir(Path(root)) / row["stamp"])
        except OSError as exc:
            _log.info("sync: could not prune snapshot %s: %s", row["stamp"], exc)


def revert(root: Path, to: str, *, stamp: str = "") -> str:
    """Restore ``to`` over ``root``. Returns the stamp the replaced state got.

    Raises ``FileNotFoundError`` for an unknown stamp BEFORE anything is
    touched: a revert that half-applies is worse than one that refuses, because
    the operator then has neither state.
    """
    root = Path(root)
    source = _dir(root) / to
    if not to or not source.is_dir():
        raise FileNotFoundError(f"no such snapshot: {to}")

    replaced = take(root, stamp or _now_stamp())
    for name in SUBTREES:
        live = root / name
        if live.is_dir():
            shutil.rmtree(live)
        src = source / name
        if src.is_dir():
            shutil.copytree(src, live, copy_function=os.link, dirs_exist_ok=True)
    _log.info("sync: reverted %s to %s (previous state kept as %s)",
              root, to, replaced)
    return replaced


__all__ = ["DIR", "SUBTREES", "keep", "take", "listing", "revert"]
