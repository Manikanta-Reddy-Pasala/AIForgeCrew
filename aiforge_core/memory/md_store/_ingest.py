"""md_store internals: writing `.md` memory files, mirroring them into the
searchable backend, section/bullet appenders, whole-dir (re)ingest and file
deletion. Depends only on `_base`."""
from __future__ import annotations

import hashlib
import re

from ._base import (
    _all_md_files,
    _brief_title,
    _find_by_source,
    _WRITE_LOCK,
    _log,
    _md_path_for_stem,
    _now_iso,
    _parse,
    _resolve_md,
    _slug,
    captures_dir,
    memory_dir,
)


def _ingest_unit(*, title: str, body: str, kind: str, tags: list[str],
                 source: str, repo: str, replace: bool = False) -> None:
    """Mirror a note into the active memory backend so it's searchable.
    ``replace=True`` deletes any prior row(s) with this ``source`` first — used
    for briefs so a re-ingest reclaims the old generation instead of piling up
    stale/orphan rows (incl. pre-scope-fix ``repo='notes'`` copies)."""
    text = f"{title}\n\n{body}".strip()
    from aiforge_core.memory import backend_select as _bsel
    try:
        if _bsel.embedded():
            from aiforge_core.memory import sqlite_memory as _sqlmem
            if replace:
                # embed-only-on-change: if the source's row already holds this
                # exact text, leave it — skip a needless delete + re-embed.
                if _sqlmem.source_text_unchanged(source, f"{title}\n\n{body}".strip()):
                    return
                _sqlmem.delete_by_source(source)
            _sqlmem.write_unit(text=text, kind=kind, source=source,
                               tags=tags, metadata={"md": True}, repo=repo)
        else:
            from aiforge_core.runtime.tools.memory_write import memory_write
            # Forward the md:* source so memory_write's brief-feed skips it —
            # capture already maintains the (topic-aware) brief for this write.
            memory_write(text=text, kind=kind, tags=tags, repo=repo,
                         source=source)
    except Exception:  # noqa: BLE001
        pass  # md file is the source of truth; DB mirror is best-effort


def write(title: str, text: str, *, kind: str = "note",
          tags: list[str] | None = None, source: str = "manual",
          repo: str = "notes", topic: str | None = None,
          ingest: bool = True) -> dict:
    """Create an md memory file + ingest it into the searchable backend.

    ``repo`` and ``topic`` are written into the frontmatter (NOT just the DB
    mirror) so the compactor can group by them — the project-brief (per-repo)
    and topic-note (per-topic) axes read the md files, so anything that isn't
    stamped here simply won't roll up into either brief.

    W5: direct callers (e.g. the manual /api note endpoint) that leave the
    default ``repo="notes"`` create an UNSCOPED note — it won't roll up into any
    project/topic brief and is invisible to repo-scoped recall (only the global
    Memory search reaches it). That's intended for hand-dropped notes; scoped
    knowledge should go through ``capture()`` (which classifies + stamps)."""
    tags = list(tags or [])
    created = _now_iso()
    digest = hashlib.sha1((title + text).encode()).hexdigest()[:6]
    stem = f"{_slug(title)}-{created[:10].replace('-', '')}-{digest}"
    # RESERVED PREFIX GUARD: never let a per-note capture start with
    # ``compacted-``. compact() EXCLUDES every ``compacted-*`` file from its
    # live set (treats it as an already-canonical brief), so a masquerading
    # capture would slip past compaction FOREVER and pile up. Strip the prefix
    # (the date+hex suffix still keeps the name unique).
    stem = re.sub(r"^compacted[-_]+", "", stem) or f"note-{digest}"
    # CONTENT DEDUP: an identical note (same title+text → same digest) captured
    # on a different day would otherwise mint a NEW dated file. If one already
    # exists, reuse it — no duplicate md file, no re-ingest.
    for _ex in list(captures_dir().glob(f"*-{digest}.md")) \
            + list(memory_dir().glob(f"*-{digest}.md")):
        try:
            _exd = _parse(_ex)
            if (_exd.get("title") or "") == title and (_exd.get("body") or "").strip() == (text or "").strip():
                _exd.pop("body", None)
                return _exd
        except Exception:  # noqa: BLE001
            continue
    path = _md_path_for_stem(stem)
    fm = (
        "---\n"
        f"title: {title}\n"
        f"kind: {kind}\n"
        f"tags: {', '.join(tags)}\n"
        f"source: {source}\n"
        f"repo: {repo or ''}\n"
        f"topic: {topic or ''}\n"
        f"created: {created}\n"
        "---\n\n"
    )
    path.write_text(fm + (text or "").strip() + "\n", encoding="utf-8")
    # ingest=False: md file only (compaction source) — used when the caller
    # already wrote this fact to the backend (e.g. the learner), so we don't
    # double-write the searchable store.
    if ingest:
        _ingest_unit(title=title, body=text, kind=kind, tags=tags,
                     source=f"md:{stem}", repo=repo)
    d = _parse(path)
    d.pop("body", None)
    return d
def upsert_section(*, source: str, title: str, section_title: str,
                   section_body: str, kind: str = "session",
                   tags: list[str] | None = None, repo: str = "notes") -> dict:
    """Append a section to the file keyed by ``source`` (create on first
    use). The filename is the FULL readable ``title`` slug — one stable
    file per session that grows with each run, then re-ingested whole.
    """
    tags = list(tags or [])
    with _WRITE_LOCK:
        existing = _find_by_source(source)
        if existing is not None:
            raw = existing.read_text(encoding="utf-8", errors="replace").rstrip()
            existing.write_text(
                raw + f"\n\n## {section_title}\n\n{section_body.strip()}\n",
                encoding="utf-8")
            path = existing
        else:
            stem = _slug(title)
            path = _md_path_for_stem(stem)
            i = 1
            while path.exists():       # different session, same title → suffix
                path = _md_path_for_stem(f"{stem}-{i}")
                i += 1
            fm = (
                "---\n"
                f"title: {title}\n"
                f"kind: {kind}\n"
                f"tags: {', '.join(tags)}\n"
                f"source: {source}\n"
                f"created: {_now_iso()}\n"
                "---\n\n"
            )
            path.write_text(fm + f"## {section_title}\n\n{section_body.strip()}\n",
                            encoding="utf-8")
    d = _parse(path)
    _ingest_unit(title=d["title"], body=d["body"], kind=kind,
                 tags=d["tags"] or tags, source=source, repo=repo)
    d.pop("body", None)
    return d


def append_bullet(*, source: str, title: str, bullet: str,
                  kind: str = "rule", tags: list[str] | None = None,
                  repo: str = "rules") -> dict:
    """Append a deduped ``- bullet`` to the file keyed by ``source`` (one
    clean list, e.g. a rule book). Used for user rules that must persist
    and apply every session."""
    tags = list(tags or [])
    line = "- " + bullet.strip()
    with _WRITE_LOCK:
        existing = _find_by_source(source)
        if existing is not None:
            raw = existing.read_text(encoding="utf-8", errors="replace").rstrip()
            # Dedup against existing BULLET LINES (exact), not a raw substring
            # — a short bullet that's a substring of a longer one is not a dup.
            if line in raw.splitlines():
                d = _parse(existing); d.pop("body", None); return d
            existing.write_text(raw + "\n" + line + "\n", encoding="utf-8")
            path = existing
        else:
            stem = _slug(title)
            path = _md_path_for_stem(stem)
            i = 1
            while path.exists():
                path = _md_path_for_stem(f"{stem}-{i}")
                i += 1
            fm = (
                "---\n"
                f"title: {title}\n"
                f"kind: {kind}\n"
                f"tags: {', '.join(tags)}\n"
                f"source: {source}\n"
                f"created: {_now_iso()}\n"
                "---\n\n"
            )
            path.write_text(fm + line + "\n", encoding="utf-8")
    d = _parse(path)
    _ingest_unit(title=d["title"], body=d["body"], kind=kind,
                 tags=d["tags"] or tags, source=source, repo=repo)
    d.pop("body", None)
    return d


def _purge_stale_brief_rows() -> None:
    """Reclaim compacted-brief rows stranded under repo='notes' before briefs
    were ingested under their real scope — else they linger as duplicates
    forever."""
    try:
        from aiforge_core.memory import backend_select as _bsel
        if not _bsel.embedded():
            return
        from aiforge_core.memory import sqlite_memory as _sqlmem
        purged = _sqlmem.delete_stale_compacted_notes()
        if purged:
            _log.info("ingest_dir: purged %d stale repo=notes brief rows", purged)
    except Exception:  # noqa: BLE001
        pass


def _brief_scope_key(stem: str) -> str:
    """The scope a compacted brief belongs to.

    A ``-N`` split-part suffix is stripped ONLY when the primary
    compacted-<base>.md exists — else a real slug ending in a number (log4j-2,
    s3-bucket-1) would be mangled to the wrong key.
    """
    base = stem[len("compacted-"):]
    m = re.match(r"^(.*)-\d+$", base)
    if m and (_resolve_md("compacted-" + m.group(1)) is not None):
        base = m.group(1)
    return base or "notes"


def _brief_unit(p, d: dict) -> tuple[dict, str, str, str, str, bool]:
    """A consolidated brief, ingested EXACTLY as compact()'s Phase-3 does —
    same kind ("knowledge") AND source ("compacted:<stem>") — so the two ingest
    paths reclaim ONE row instead of storing the same brief twice. The
    mechanical 'compacted' kind used to show up as the label in search/UI, and
    the compacted-<key> stem as the title; both are replaced here. Envelope
    stripped so recall vectors carry knowledge, not boilerplate.
    """
    repo = _brief_scope_key(p.stem)
    body = d["body"]
    try:
        from aiforge_core.runtime import work_notes
        body = work_notes.knowledge_text(d["body"])
    except Exception:  # noqa: BLE001
        pass
    return ({**d, "title": _brief_title(repo)}, body, "knowledge",
            f"compacted:{p.stem}", repo, True)     # replace → reclaim the row


def _ingest_one_md(p) -> str | None:
    """Ingest one md file; returns the source it was stored under."""
    d = _parse(p)
    if p.stem.startswith("compacted-"):
        d, body, kind, source, repo, replace = _brief_unit(p, d)
    else:
        body, kind = d["body"], d["kind"]
        source, repo, replace = f"md:{p.stem}", (d.get("repo") or "notes"), False
    _ingest_unit(title=d["title"], body=body, kind=kind, tags=d["tags"],
                 source=source, repo=repo, replace=replace)
    return source


def _prune_deleted(present_sources: set) -> int:
    """RECONCILE: md is the source of truth — prune index rows whose md file was
    DELETED or archived (create/update is already handled by the ingest)."""
    try:
        from aiforge_core.memory import backend_select as _bsel
        if not _bsel.embedded():
            return 0
        from aiforge_core.memory import sqlite_memory as _sqlmem
        pruned = _sqlmem.prune_missing_file_rows(present_sources)
        if pruned:
            _log.info("ingest_dir: pruned %d orphan rows (md deleted)", pruned)
        return pruned
    except Exception:  # noqa: BLE001
        return 0


def ingest_dir() -> dict:
    """(Re)ingest every md file in the memory dir into the backend.

    For files dropped in by hand. Dedup is handled by the backend's own
    content hashing, so re-running is safe.
    """
    _purge_stale_brief_rows()
    n = 0
    present_sources: set[str] = set()   # sources of files on disk → for reconcile
    for p in _all_md_files():
        try:
            source = _ingest_one_md(p)
        except Exception:  # noqa: BLE001
            continue
        present_sources.add(source)
        n += 1
    return {"ok": True, "ingested": n, "pruned": _prune_deleted(present_sources),
            "dir": str(memory_dir())}


def delete_file(name: str) -> bool:
    p = _resolve_md(name)
    if p and p.is_file():
        stem = p.stem
        p.unlink()
        # Sync the vector index: drop this file's row(s) immediately (md is the
        # source of truth — a deleted file must not linger in search).
        try:
            from aiforge_core.memory import backend_select, sqlite_memory
            if backend_select.embedded():
                src = (f"compacted:{stem}" if stem.startswith("compacted-")
                       else f"md:{stem}")
                sqlite_memory.delete_by_source(src)
        except Exception:  # noqa: BLE001
            pass
        return True
    return False
