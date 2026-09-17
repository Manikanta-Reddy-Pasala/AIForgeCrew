"""Sweeping stale captures and empty topic briefs out of the store."""
from __future__ import annotations

import re

from ._base import (
    _COMPACT_LOCK,
    _now_iso,
    iter_briefs,
    memory_dir,
)

# Per-run capture signature: <slug>-YYYYMMDD-<6hex>.md
_CAPTURE_SIG_SUFFIX_RE = re.compile(r"-\d{8}-[0-9a-f]{6}\.md$")


def _retire_brief(path, dst, archive: bool) -> bool:
    """Move the file into ``dst`` (reversible), or delete it. False on failure."""
    import shutil
    try:
        if archive:
            dst.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dst / path.name))
        else:
            path.unlink()
        return True
    except OSError:
        return False


def sweep_stale_captures(*, archive: bool = True) -> dict:
    """Retire per-run capture files that MASQUERADE as canonical briefs.

    A capture is stamped ``<slug>-YYYYMMDD-<6hex>.md``. When its title happened
    to start with "compacted" (e.g. the legacy-cleanup re-writing a brief's own
    title) the slug became ``compacted-…`` — and ``compact()`` excludes every
    ``compacted-*`` file from its live set, so these transient captures slip
    past compaction FOREVER and accumulate (``compacted-retry-on-empty-fix`` &
    friends). Their facts are already folded into the real
    ``compacted-<topic>.md`` brief by ``_brief_upsert`` at write time, so they
    carry nothing new.

    Moves each masquerader into ``archive/<ts>/`` (reversible; ``archive=False``
    deletes). Canonical briefs — ``compacted-<topic>.md`` with NO date-hex
    suffix — are untouched. Runs in the hourly compaction. Never raises."""
    swept: list[str] = []
    dst = memory_dir() / "archive" / _now_iso().replace(":", "")
    try:
        with _COMPACT_LOCK:
            for p in iter_briefs():
                if not _CAPTURE_SIG_SUFFIX_RE.search(p.name):
                    continue                    # real canonical brief — keep
                if _retire_brief(p, dst, archive):
                    swept.append(p.name)
    except Exception as exc:  # noqa: BLE001 — sweep is best-effort upkeep
        return {"ok": False, "error": str(exc), "swept": len(swept)}
    return {"ok": True, "swept": len(swept), "archived": archive,
            "files": swept}


def _is_dead_brief(path) -> bool:
    """A brief carrying ONLY the boilerplate Objective.

    Links matter here: map_scopes links are BIDIRECTIONAL, so deleting a
    links-only brief orphans its sibling's inbound link.
    """
    from aiforge_core.runtime import work_notes
    try:
        parsed = work_notes.parse_note(
            path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return False
    sec = parsed.get("sections") or {}
    return not (sec.get("facts") or sec.get("learnings")
                or sec.get("key_results") or sec.get("links")
                or (parsed.get("body") or "").strip())


def sweep_empty_briefs(*, archive: bool = True) -> dict:
    """Retire DEAD canonical briefs — a ``compacted-<key>.md`` that carries only
    the boilerplate Objective with NO Facts, Key results, Learnings, or body.

    These accumulate when a topic's facts all migrate into another brief (the
    labeller re-clusters), when a fact-only brief is emptied, or from legacy
    ``compacted-compacted-*`` double-fold artifacts — leaving a stub that shows
    up as an "empty" memory but holds no knowledge. Moves each into
    ``archive/<ts>/`` (reversible; ``archive=False`` deletes). A brief with ANY
    real content is never touched. Never raises."""
    swept: list[str] = []
    dst = memory_dir() / "archive" / _now_iso().replace(":", "")
    try:
        with _COMPACT_LOCK:
            for p in iter_briefs():
                if _CAPTURE_SIG_SUFFIX_RE.search(p.name):
                    continue          # a capture — sweep_stale_captures owns it
                if _is_dead_brief(p) and _retire_brief(p, dst, archive):
                    swept.append(p.name)
    except Exception as exc:  # noqa: BLE001 — best-effort upkeep
        return {"ok": False, "error": str(exc), "swept": len(swept)}
    return {"ok": True, "swept": len(swept), "archived": archive, "files": swept}


def _demote_headings(body: str, by: int = 2) -> str:
    """Push every markdown heading in ``body`` ``by`` levels deeper (capped at
    h6) so an embedded ``# Title`` doesn't collide with the ``##`` section
    wrapper a compacted file gives each source note."""
    out = []
    for line in body.splitlines():
        m = re.match(r"^(#{1,6})(\s)", line)
        if m:
            lvl = min(6, len(m.group(1)) + by)
            out.append("#" * lvl + line[len(m.group(1)):])
        else:
            out.append(line)
    return "\n".join(out)
