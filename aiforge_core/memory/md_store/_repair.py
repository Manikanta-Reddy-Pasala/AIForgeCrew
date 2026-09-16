"""md_store internals: retire captures that were never facts, and collapse the
duplicates the old append-only writer left behind.

`capture()` now refuses a fragment at the door, but the store already holds
years of them — CLI usage lines, table rows, raw chat turns, and the same claim
saved three times at three truncation lengths. Compaction runs this pass so the
store repairs itself instead of needing a one-off script per box.
"""
from __future__ import annotations

import shutil

from . import _fact, _subject
from ._base import (
    _COMPACT_LOCK,
    _capture_md_files,
    _log,
    _now_iso,
    _parse,
)


def _archive_dir():
    from ._base import memory_dir
    return memory_dir() / "archive" / f"repair-{_now_iso().replace(':', '')}"


def _retire(path, dst, archive: bool) -> bool:
    """Move (or delete) one note and drop its row from the searchable mirror."""
    try:
        from ._ingest import delete_file
        if archive:
            dst.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dst / path.name))
        else:
            delete_file(path.name)
            return True
    except Exception as exc:  # noqa: BLE001 — repair is best-effort upkeep
        _log.debug("repair: could not retire %s: %s", path.name, exc)
        return False
    try:
        from aiforge_core.memory import sqlite_memory as _sqlmem
        _sqlmem.delete_by_source(f"md:{path.stem}")
    except Exception:  # noqa: BLE001 — md file is the source of truth
        pass
    return True


def _junk_reasons(d: dict) -> list[str]:
    """Why this whole note is not a fact (empty list = keep it).

    A note is judged on its CLAIMS: one bad bullet in an otherwise good note is
    dropped by :func:`_dedupe_claims`, but a note whose every claim is a
    fragment has nothing worth keeping.
    """
    claims = _subject.claims_of(d.get("body") or "")
    if not claims:
        return ["empty note"]
    per_claim = [_fact.issues(c) for c in claims]
    if all(reasons for reasons in per_claim):
        return sorted({r for reasons in per_claim for r in reasons})
    return []


def _dedupe_claims(path, d: dict) -> int:
    """Re-fold a note's own claims through the merge rules. Returns how many
    claims were dropped (a truncation ladder collapses to its fullest member)."""
    from ._ingest import rewrite_body

    claims = _subject.claims_of(d.get("body") or "")
    kept: list[str] = []
    for c in claims:
        if _fact.issues(c):
            continue                      # a bad bullet inside a good note
        kept, _ = _subject.merge_claim(kept, c)
    if kept == claims:
        return 0
    rewrite_body(path, _subject.render_claims(kept))
    return len(claims) - len(kept)


def repair_captures(*, archive: bool = True, dry_run: bool = False,
                    limit: int | None = None) -> dict:
    """Drop non-fact captures and collapse duplicate claims. Never raises.

    ``dry_run`` reports what would go without touching anything — always worth
    running first on a store you care about, since retirement is reversible only
    while ``archive`` is True.
    """
    retired: list[dict] = []
    collapsed = 0
    scanned = 0
    dst = _archive_dir()
    try:
        with _COMPACT_LOCK:
            for path in _capture_md_files():
                if limit is not None and scanned >= limit:
                    break
                scanned += 1
                try:
                    d = _parse(path)
                except Exception:  # noqa: BLE001 — unreadable note, leave it
                    continue
                reasons = _junk_reasons(d)
                if reasons:
                    if dry_run or _retire(path, dst, archive):
                        retired.append({"file": path.name, "reasons": reasons,
                                        "title": d.get("title") or ""})
                    continue
                if not dry_run:
                    collapsed += _dedupe_claims(path, d)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "scanned": scanned,
                "retired": len(retired), "collapsed": collapsed}
    if retired or collapsed:
        _log.info("repair: scanned %d, retired %d non-fact note(s), collapsed "
                  "%d duplicate claim(s)%s", scanned, len(retired), collapsed,
                  " [dry-run]" if dry_run else "")
    return {"ok": True, "scanned": scanned, "retired": len(retired),
            "collapsed": collapsed, "archived": archive and not dry_run,
            "dry_run": dry_run, "files": retired[:200]}


__all__ = ["repair_captures"]
