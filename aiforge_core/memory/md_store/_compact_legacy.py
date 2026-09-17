"""Cleaning up briefs written by older compaction code."""
from __future__ import annotations

import re

from ._base import (
    _COMPACT_LOCK,
    _log,
    _now_iso,
    _resolve_md,
    iter_briefs,
    memory_dir,
)
from ._capture import capture


def _pkg():
    """The parent module, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.memory.md_store._compact as package
    return package


# Compacted files whose KEY is not a real topic — id-keyed briefs (chat run in
# a jira/confluence context / session scratch produced these) and per-kind
# blobs. Their knowledge is re-captured as topic units then the file archived,
# so a topic compaction re-folds them into meaningful topic briefs.
_CRYPTIC_KEY_RE = re.compile(
    r"^(?:\d{4,}|[a-z]{2,5}-\d+|session-\d+|"
    r"session|project|project-learning|learning|chat-summary|notes|compacted)$",
    re.IGNORECASE)


def _is_bug_artifact(fm: dict) -> bool:
    """A compacted-* file that is NOT a proper kind=knowledge brief (a stray
    kind=note unit written under a compacted name, or a source starting
    'agent:') — fold + archive it; the real topic brief is regenerated on the
    next compaction."""
    return bool((fm.get("kind") and fm.get("kind") != "knowledge")
                or str(fm.get("source") or "").startswith("agent:"))


def _is_live_split_part(base: str) -> bool:
    """A split overflow part (compacted-<topic>-N) is NOT stale when its topic's
    primary brief exists and that topic name isn't itself cryptic — e.g. keep
    compacted-auth-2.md (topic 'auth'), but still flag a truly cryptic
    compacted-clr-3049.md (no compacted-clr.md primary)."""
    m = re.match(r"^(.*)-\d+$", base)
    return bool(m and not _CRYPTIC_KEY_RE.match(m.group(1))
                and _resolve_md("compacted-" + m.group(1)) is not None)


def _stale_briefs() -> list:
    from aiforge_core.runtime import work_notes
    stale: list = []
    for pth in iter_briefs():
        try:
            fm = work_notes.parse_note(
                pth.read_text(encoding="utf-8", errors="replace"))["frontmatter"]
        except Exception:  # noqa: BLE001
            fm = {}
        if _is_bug_artifact(fm):
            stale.append(pth)
            continue
        base = pth.stem[len("compacted-"):]
        if _is_live_split_part(base):
            continue
        if _CRYPTIC_KEY_RE.match(base):
            stale.append(pth)
    return stale


def _facts_to_recapture(pth, parsed: dict) -> list:
    """The brief's Facts — or, for a legacy per-kind blob that keeps knowledge
    in the BODY rather than Facts, the body's knowledge lines."""
    from aiforge_core.runtime import work_notes
    facts = list(parsed["sections"].get("facts") or [])
    if facts:
        return facts
    body_know = work_notes.knowledge_text(
        pth.read_text(encoding="utf-8", errors="replace"))
    return [ln.lstrip("-* ").strip() for ln in body_know.splitlines()
            if ln.strip() and not ln.startswith("#")][:200]


def _recapture_kind(parsed: dict) -> str:
    """The kind a legacy brief's facts should be re-captured under.

    Hardcoding ``topic_learning`` is what stamped EVERY re-captured fact as a
    topic learning — whole stores show one kind on every row. A brief's own
    ``type`` is its ENVELOPE kind (``knowledge``/``compacted``), never a capture
    kind, so the honest source is the capture kind carried in its tags; failing
    that a plain ``learning``, which is the one kind that claims nothing about
    scope.
    """
    from ._capture import _CAPTURE_KINDS
    fm = parsed.get("frontmatter") or {}
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    for t in tags:
        if str(t).strip() in _CAPTURE_KINDS:
            return str(t).strip()
    return "learning"


def _fold_one_stale(pth, archive) -> tuple[int, bool]:
    """Re-capture the brief's facts as topic units and archive it (reversible).
    Returns ``(facts_moved, folded)``."""
    import shutil

    from aiforge_core.runtime import work_notes
    try:
        parsed = work_notes.parse_note(
            pth.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return 0, False
    moved = 0
    refused = 0
    for f in _facts_to_recapture(pth, parsed):
        if not f.strip():
            continue
        try:
            # repo=None ⇒ the SHARED (global) scope. Re-capturing these under
            # "notes" fed a loop: the repo axis minted compacted-notes.md, whose
            # key is itself "cryptic", so the next cleanup folded it and
            # re-captured every fact again — one classify call per fact, forever.
            res = capture(_recapture_kind(parsed), f.strip(), repo=None,
                          source="cleanup:legacy-compacted")
            # capture() now REFUSES a non-fact instead of raising, so counting
            # unconditionally reported facts as migrated that were dropped —
            # and the brief they came from was archived anyway.
            if not (isinstance(res, dict) and res.get("skipped")):
                moved += 1
            else:
                refused += 1
        except Exception:  # noqa: BLE001
            pass
    if moved == 0 and refused:
        # Every fact in this brief was refused. Archiving it now would delete
        # them from the live store with nothing carried over, so leave it.
        _log.info("tidy-legacy: keeping %s — all %d fact(s) refused by the gate",
                  pth.name, refused)
        return 0, False
    try:
        shutil.move(str(pth), str(archive / pth.name))
        return moved, True
    except Exception:  # noqa: BLE001
        return moved, False


def cleanup_legacy_compacted(*, dry_run: bool = False,
                             model_role: str | None = None,
                             refold: bool = True, progress=None) -> dict:
    """One-time tidy: fold id-keyed / per-kind ``compacted-*`` briefs back into
    the TOPIC axis. Each stale file's Facts are re-captured as topic units (no
    forced topic → the labeller re-clusters them), the original is archived
    (reversible), then a topic compaction re-folds everything into meaningful,
    tagged, split-aware topic briefs. ``dry_run`` reports the plan only."""
    stale = _stale_briefs()
    if dry_run:
        return {"ok": True, "dry_run": True,
                "stale": sorted(p.name for p in stale), "count": len(stale)}
    if not stale:
        _log.info("tidy-legacy: no cryptic/id-named briefs to fold")
        return {"ok": True, "dry_run": False, "folded": 0, "facts": 0,
                "note": "no id-keyed / per-kind compacted files to clean"}
    _log.info("tidy-legacy: folding %d cryptic/id-named brief(s)%s",
              len(stale), " + re-compacting" if refold else "")
    archive = memory_dir() / "archive" / ("cleanup-" + _now_iso().replace(":", ""))
    facts_moved = 0
    folded = 0
    with _COMPACT_LOCK:
        archive.mkdir(parents=True, exist_ok=True)
        for pth in stale:
            moved, ok = _fold_one_stale(pth, archive)
            facts_moved += moved
            folded += 1 if ok else 0
    # Re-fold the re-captured units into meaningful topic briefs — SKIPPED when
    # the caller (e.g. 'compact all') runs its own topic pass right after, so the
    # heavy LLM consolidation isn't done twice.
    topic = (_pkg().compact(group_by="topic", min_group=1, summarize=True,
                     model_role=model_role, archive_sources=True,
                     progress=progress) if refold else None)
    return {"ok": True, "dry_run": False, "folded": folded,
            "facts": facts_moved, "archive": str(archive),
            "topic_compact": topic}
