"""md_store internals: retire captures that were never facts, and collapse the
duplicates the old append-only writer left behind.

`capture()` now refuses a fragment at the door, but the store already holds
years of them — CLI usage lines, table rows, raw chat turns, and the same claim
saved three times at three truncation lengths. Compaction runs this pass so the
store repairs itself instead of needing a one-off script per box.

This pass DELETES, on a store a human also writes to by hand, so it is
deliberately narrow: it touches only per-fact capture notes (never a session
log, a rule file or a hand-dropped page), and it only rewrites a body it can
reconstruct exactly — anything with prose, headings or code around the bullets
is left alone rather than reduced to its bullets.
"""
from __future__ import annotations

import itertools
import shutil

from . import _fact, _subject
from ._base import (
    _COMPACT_LOCK,
    _WRITE_LOCK,
    _capture_md_files,
    _log,
    _now_iso,
    _parse,
)
from ._capture import _CAPTURE_KINDS


def _archive_dir():
    from ._base import memory_dir
    return memory_dir() / "archive" / f"repair-{_now_iso().replace(':', '')}"


def _is_fact_note(d: dict) -> bool:
    """Only a per-fact capture is this pass's business.

    ``_capture_md_files()`` also returns session logs (``kind: session``, whose
    body is a series of ``## Run N`` sections), rule files, and anything a human
    dropped into the memory dir. Judging those by the fact rules retires them on
    the first heading character.
    """
    return (d.get("kind") or "") in _CAPTURE_KINDS


def _junk_reasons(d: dict) -> list[str]:
    """Why this whole note is not a fact (empty list = keep it).

    A note is judged on its CLAIMS: one bad bullet in an otherwise good note is
    handled by :func:`_dedupe_claims`, but a note whose every claim is a
    fragment has nothing worth keeping.
    """
    if not _is_fact_note(d):
        return []
    claims = _subject.claims_of(d.get("body") or "")
    if not claims:
        return ["empty note"]
    per_claim = [_fact.structural_issues(c) for c in claims]
    if not all(per_claim):
        return []
    return sorted(set(itertools.chain.from_iterable(per_claim)))


def _retire(path, dst, archive: bool) -> bool:
    """Move (or delete) one note and drop its row from the searchable mirror.

    Takes ``_WRITE_LOCK`` and re-reads the note first: a chat turn can fold a
    brand-new claim into this file between the scan and the move, and archiving
    it then would take that fact with it.
    """
    from ._ingest import delete_file
    with _WRITE_LOCK:
        try:
            if not path.exists():
                return False
            if not _junk_reasons(_parse(path)):
                _log.info("repair: %s changed under us — keeping it", path.name)
                return False
            if archive:
                dst.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(dst / path.name))
            elif not delete_file(path.name):
                return False
        except Exception as exc:  # noqa: BLE001  # repair is best-effort upkeep
            _log.debug("repair: could not retire %s: %s", path.name, exc)
            return False
    try:
        from aiforge_core.memory import sqlite_memory as _sqlmem
        _sqlmem.delete_by_source(f"md:{path.stem}")
    except Exception:  # noqa: BLE001  # md file is the source of truth
        pass
    return True


def _rewritable(body: str, claims: list[str]) -> bool:
    """True when the body is EXACTLY its bullet list.

    Rewriting from the claims alone drops everything the claim list does not
    carry. A note like::

        Key findings from the outage investigation:

        - the change stream health check runs every 120 seconds

    would come back as that one bullet, losing the paragraph — and an in-place
    rewrite is NOT covered by the archive. So a body that does not round-trip is
    left exactly as it is.
    """
    return _subject.render_claims(claims).strip() == (body or "").strip()


def _dedupe_claims(path, d: dict, dst, archive: bool) -> tuple[int, list]:
    """Re-fold a note's own claims through the merge rules.

    Returns ``(dropped, detail)``. Dropping a bullet inside a SURVIVING note is
    not covered by retirement's archive — the file stays, the line goes — so a
    copy is archived first and every drop is reported and logged.
    """
    from ._ingest import rewrite_body

    body = d.get("body") or ""
    claims = _subject.claims_of(body)
    if not _rewritable(body, claims):
        return 0, []
    kept: list[str] = []
    detail: list[dict] = []
    for c in claims:
        why = _fact.structural_issues(c)
        if why:
            detail.append({"file": path.name, "claim": c[:120],
                           "why": "; ".join(why)})
            continue                      # a bad bullet inside a good note
        before = len(kept)
        kept, action = _subject.merge_claim(kept, c)
        if len(kept) <= before and action != "added":
            detail.append({"file": path.name, "claim": c[:120], "why": action})
    if kept == claims:
        return 0, []
    if archive:
        try:
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(path), str(dst / f"pre-collapse-{path.name}"))
        except Exception as exc:  # noqa: BLE001  # no copy, no rewrite
            _log.debug("repair: could not archive %s before collapse: %s",
                       path.name, exc)
            return 0, []
    for row in detail:
        _log.info("repair: dropped claim from %s (%s): %r",
                  row["file"], row["why"], row["claim"])
    rewrite_body(path, _subject.render_claims(kept))
    return len(claims) - len(kept), detail


def _repair_one(path, dst, archive: bool,
                dry_run: bool) -> tuple[dict | None, int, list]:
    """``(retired_row, collapsed, dropped_detail)`` for one note."""
    try:
        d = _parse(path)
    except Exception:  # noqa: BLE001  # unreadable note, leave it
        return None, 0, []
    reasons = _junk_reasons(d)
    if reasons:
        if dry_run or _retire(path, dst, archive):
            return ({"file": path.name, "reasons": reasons,
                     "title": d.get("title") or ""}, 0, [])
        return None, 0, []
    if dry_run or not _is_fact_note(d):
        return None, 0, []
    n, detail = _dedupe_claims(path, d, dst, archive)
    return None, n, detail


def repair_captures(*, archive: bool = True, dry_run: bool = False,
                    limit: int | None = None) -> dict:
    """Drop non-fact captures and collapse duplicate claims. Never raises.

    ``dry_run`` reports what would be retired without touching anything —
    always worth running first on a store you care about.
    """
    retired: list[dict] = []
    dropped_claims: list[dict] = []
    collapsed = 0
    scanned = 0
    dst = _archive_dir()
    try:
        with _COMPACT_LOCK:
            for path in _capture_md_files():
                if limit is not None and scanned >= limit:
                    break
                scanned += 1
                row, n, detail = _repair_one(path, dst, archive, dry_run)
                if row:
                    retired.append(row)
                collapsed += n
                dropped_claims.extend(detail)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "scanned": scanned,
                "retired": len(retired), "collapsed": collapsed}
    if retired or collapsed:
        _log.info("repair: scanned %d, retired %d non-fact note(s), collapsed "
                  "%d duplicate claim(s)%s", scanned, len(retired), collapsed,
                  " [dry-run]" if dry_run else "")
    return {"ok": True, "scanned": scanned, "retired": len(retired),
            "collapsed": collapsed, "archived": archive and not dry_run,
            "dry_run": dry_run, "files": retired[:200],
            "dropped_claims": dropped_claims[:200]}


__all__ = ["repair_captures"]
