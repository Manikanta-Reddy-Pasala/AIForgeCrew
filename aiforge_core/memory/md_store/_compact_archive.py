"""Archiving captures a brief already covers, and pruning old archives."""
from __future__ import annotations

import os
import re

from ._base import (
    _COMPACT_LOCK,
    _WRITE_LOCK,
    _capture_md_files,
    _log,
    _now_iso,
    memory_dir,
)


def _pkg():
    """The parent module, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.memory.md_store._compact as package
    return package


def archive_covered_captures() -> dict:
    """Archive local captures that an ARRIVED brief already claims to have eaten.

    Ordinarily a machine archives the captures its OWN fold just consumed. This
    is the other half: a brief that arrived from elsewhere, claiming captures we
    also hold, lets us tidy them up without re-distilling them.

    Soft-fails CLOSED, the opposite of the distillation gate, and deliberately:
    archiving a capture no brief covers destroys an un-distilled memory and
    nothing can rebuild it, while failing to archive one that IS covered leaves
    a tidy-up for the next cycle. Any doubt at all → move nothing.
    """
    import shutil
    try:
        covered = _pkg().brief_source_stems()
    except Exception as exc:  # noqa: BLE001 — see docstring: uncertainty ⇒ nothing
        _log.info("compact: provenance unreadable (%s) — archiving nothing", exc)
        return {"archived": 0, "housekeeping": "provenance-unreadable"}
    if not covered:
        # No brief claims anything (e.g. every brief predates provenance) —
        # nothing is PROVABLY distilled, so nothing may be moved.
        return {"archived": 0}

    dst = memory_dir() / "archive" / _now_iso().replace(":", "")
    moved: list[str] = []
    with _COMPACT_LOCK, _WRITE_LOCK:
        for p in _capture_md_files():
            if p.stem not in covered:
                continue
            try:
                dst.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(dst / p.name))
                moved.append(p.name)
            except OSError:      # keep the capture; the next cycle retries
                continue
    if moved:
        _log.info("compact: archived %d capture(s) already covered by a brief",
                  len(moved))
    return {"archived": len(moved), "archived_files": moved,
            "archive": str(dst)}


# How long a retired capture/brief stays readable in archive/ before it is
# removed. Everything in there has already been folded into a brief, so this is
# a copy — but it is the copy you reach for when a fold went wrong, which is why
# the default is months rather than days. 0 disables the sweep entirely.
_ARCHIVE_KEEP_DAYS = 180
# archive/<stamp>/ — written by _now_iso() with the colons stripped. Only a
# folder shaped like that is ever removed; anything else a human put in
# archive/ is left alone.
_ARCHIVE_STAMP_RE = re.compile(r"^(?:cleanup-)?\d{4}-\d{2}-\d{2}T\d{6}")


def _expired_archive_dirs(root, cutoff: float):
    """Archive folders older than ``cutoff``. Anything not named like a stamp is
    not ours to remove, and an unreadable entry is left alone."""
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not _ARCHIVE_STAMP_RE.match(d.name):
            continue
        try:
            if d.stat().st_mtime < cutoff:
                yield d
        except OSError:
            continue


def prune_archive(*, days: "int | None" = None, dry_run: bool = False) -> dict:
    """Remove archive folders older than the retention window.

    The archive had no retention at all: every compaction moved its consumed
    captures in and nothing ever took them out, so the one folder guaranteed to
    grow without bound was the one holding copies."""
    import shutil
    import time
    if days is None:
        try:
            days = int(os.environ.get("AIFORGE_ARCHIVE_KEEP_DAYS",
                                      _ARCHIVE_KEEP_DAYS))
        except (TypeError, ValueError):
            days = _ARCHIVE_KEEP_DAYS
    root = memory_dir() / "archive"
    if days <= 0 or not root.is_dir():
        return {"ok": True, "removed": 0, "skipped": "disabled"}
    cutoff = time.time() - days * 86400
    removed: list[str] = []
    for d in _expired_archive_dirs(root, cutoff):
        try:
            if not dry_run:
                shutil.rmtree(d)
            removed.append(d.name)
        except OSError:
            continue
    if removed:
        _log.info("compact: pruned %d archive folder(s) older than %d days",
                  len(removed), days)
    return {"ok": True, "removed": len(removed), "folders": removed,
            "days": days, "dry_run": dry_run}
