"""Markdown-file memory: human-readable notes on the filesystem that are
ALSO ingested into the searchable memory backend and shown in the UI.

Why: the app's knowledge memory (SQLite/Neo4j) is opaque — you can't `cat`
it or diff it in git. This keeps a plain ``.md`` file per memory under a
directory (``AIFORGE_MEMORY_MD_DIR``, default ``~/.aiforge/memory``) as the
human-facing source of truth, and mirrors each into the memory backend
(via the embedded SQLite store or AFM/Neo4j) so search + stats + the
Memory tab pick it up. Drop a ``.md`` file in the dir by hand and call
:func:`ingest_dir` to pull it in.

Each file carries YAML-ish frontmatter (name/kind/tags/source/created)
followed by the markdown body.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import threading
from pathlib import Path

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

# Serialize read-modify-write on the .md files — two concurrent chat turns
# (or a chat turn + the auto-memory upsert) writing the same source file
# would otherwise lose one update (both read the same raw, both append, last
# write wins). Covers concurrency within this process.
_WRITE_LOCK = threading.Lock()


def memory_dir() -> Path:
    raw = os.environ.get("AIFORGE_MEMORY_MD_DIR") or "~/.aiforge/memory"
    p = Path(os.path.expanduser(raw))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _slug(title: str, maxlen: int = 80) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "note").lower()).strip("-")
    return (s or "note")[:maxlen]


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _parse(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta: dict = {}
    body = raw
    m = _FM_RE.match(raw)
    if m:
        body = m.group(2).strip()
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    return {
        "name": path.stem,
        "file": path.name,
        "title": meta.get("title") or meta.get("name") or path.stem,
        "kind": meta.get("kind") or "note",
        "tags": [t for t in (meta.get("tags") or "").split(",") if t.strip()],
        "source": meta.get("source") or "manual",
        "created": meta.get("created") or "",
        "preview": body[:240],
        "body": body,
        "path": str(path),
    }


def list_files() -> list[dict]:
    """All md memories (newest first), without full body."""
    out = []
    for p in sorted(memory_dir().glob("*.md"), reverse=True):
        try:
            d = _parse(p)
            d.pop("body", None)
            d["mtime"] = p.stat().st_mtime
            out.append(d)
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda d: d.get("created") or "", reverse=True)
    return out


def read_file(name: str) -> dict | None:
    p = memory_dir() / (name if name.endswith(".md") else f"{name}.md")
    if not p.is_file() or p.parent != memory_dir():
        return None
    return _parse(p)


def _ingest_unit(*, title: str, body: str, kind: str, tags: list[str],
                 source: str, repo: str) -> None:
    """Mirror a note into the active memory backend so it's searchable."""
    text = f"{title}\n\n{body}".strip()
    from aiforge_core.memory import backend_select as _bsel
    try:
        if _bsel.embedded():
            from aiforge_core.memory import sqlite_memory as _sqlmem
            _sqlmem.write_unit(text=text, kind=kind, source=source,
                               tags=tags, metadata={"md": True}, repo=repo)
        else:
            from aiforge_core.runtime.tools.memory_write import memory_write
            memory_write(text=text, kind=kind, tags=tags, repo=repo)
    except Exception:  # noqa: BLE001
        pass  # md file is the source of truth; DB mirror is best-effort


def write(title: str, text: str, *, kind: str = "note",
          tags: list[str] | None = None, source: str = "manual",
          repo: str = "notes") -> dict:
    """Create an md memory file + ingest it into the searchable backend."""
    tags = list(tags or [])
    created = _now_iso()
    digest = hashlib.sha1((title + text).encode()).hexdigest()[:6]
    stem = f"{_slug(title)}-{created[:10].replace('-', '')}-{digest}"
    path = memory_dir() / f"{stem}.md"
    fm = (
        "---\n"
        f"title: {title}\n"
        f"kind: {kind}\n"
        f"tags: {', '.join(tags)}\n"
        f"source: {source}\n"
        f"created: {created}\n"
        "---\n\n"
    )
    path.write_text(fm + (text or "").strip() + "\n", encoding="utf-8")
    _ingest_unit(title=title, body=text, kind=kind, tags=tags,
                 source=f"md:{stem}", repo=repo)
    d = _parse(path)
    d.pop("body", None)
    return d


def _find_by_source(source: str) -> Path | None:
    """Locate the md file whose frontmatter ``source`` matches (the stable
    key for a session), so repeated runs UPDATE one file instead of
    spawning a new hashed file every time."""
    for p in memory_dir().glob("*.md"):
        try:
            if _parse(p).get("source") == source:
                return p
        except Exception:  # noqa: BLE001
            continue
    return None


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
            path = memory_dir() / f"{stem}.md"
            i = 1
            while path.exists():       # different session, same title → suffix
                path = memory_dir() / f"{stem}-{i}.md"
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
            path = memory_dir() / f"{stem}.md"
            i = 1
            while path.exists():
                path = memory_dir() / f"{stem}-{i}.md"
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


def ingest_dir() -> dict:
    """(Re)ingest every md file in the memory dir into the backend.

    For files dropped in by hand. Dedup is handled by the backend's own
    content hashing, so re-running is safe.
    """
    n = 0
    for p in memory_dir().glob("*.md"):
        try:
            d = _parse(p)
            _ingest_unit(title=d["title"], body=d["body"], kind=d["kind"],
                         tags=d["tags"], source=f"md:{p.stem}", repo="notes")
            n += 1
        except Exception:  # noqa: BLE001
            continue
    return {"ok": True, "ingested": n, "dir": str(memory_dir())}


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


def _group_key(d: dict, group_by: str) -> str:
    if group_by == "tag":
        return (d["tags"][0] if d.get("tags") else "untagged")
    if group_by == "source":
        # the leading token of the source key (e.g. "chat", "md", "ticket")
        return (d.get("source") or "manual").split(":", 1)[0].split("-", 1)[0]
    return d.get("kind") or "note"


def compact(*, group_by: str = "kind", min_group: int = 2,
            dry_run: bool = False) -> dict:
    """Consolidate the sprawl of per-session ``.md`` memories into ONE
    standardized file per group, so the Memory folder stays legible.

    Grouping key (``group_by``): ``kind`` (default), ``tag``, or ``source``.
    Only groups with at least ``min_group`` files are compacted; singletons
    are left alone. Each source note becomes a ``## <title>`` section (its own
    headings demoted to fit) under a ``# <Group> memory (compacted)`` file
    with standard frontmatter. Originals are MOVED into
    ``<memory>/archive/<ts>/`` (reversible — never deleted) and the merged
    file is re-ingested into the searchable backend.

    ``dry_run`` returns the plan (group → file count) without touching disk.
    """
    import shutil

    files: list[dict] = []
    for p in memory_dir().glob("*.md"):
        try:
            d = _parse(p)
            d["_path"] = p
            files.append(d)
        except Exception:  # noqa: BLE001
            continue

    groups: dict[str, list[dict]] = {}
    for d in files:
        # never fold an already-compacted file back into a group (idempotent)
        if d["file"].startswith("compacted-"):
            continue
        groups.setdefault(_group_key(d, group_by), []).append(d)

    planned = {k: v for k, v in groups.items() if len(v) >= min_group}

    if dry_run:
        return {
            "ok": True, "dry_run": True, "group_by": group_by,
            "groups": {k: len(v) for k, v in sorted(planned.items())},
            "files_in": sum(len(v) for v in planned.values()),
            "files_out": len(planned),
        }

    if not planned:
        return {"ok": True, "dry_run": False, "group_by": group_by,
                "groups": {}, "files_in": 0, "files_out": 0,
                "note": "nothing to compact (no group ≥ min_group)"}

    archive = memory_dir() / "archive" / _now_iso().replace(":", "")
    out_files: list[str] = []
    moved = 0
    with _WRITE_LOCK:
        archive.mkdir(parents=True, exist_ok=True)
        for key, items in sorted(planned.items()):
            items.sort(key=lambda d: d.get("created") or "")
            sections = []
            for d in items:
                meta = (f"_source: {d.get('source') or 'manual'} · "
                        f"created: {d.get('created') or '?'}_")
                sections.append(
                    f"## {d['title']}\n\n{meta}\n\n"
                    f"{_demote_headings(d['body']).strip()}".rstrip())
            merged = "\n\n---\n\n".join(sections)
            created = _now_iso()
            all_tags = sorted({t for d in items for t in d.get("tags") or []})
            title = f"{key.replace('-', ' ').strip().capitalize()} memory (compacted)"
            stem = f"compacted-{_slug(key)}"
            path = memory_dir() / f"{stem}.md"
            # Re-compaction: fold new sections under the existing consolidated
            # file rather than spawning compacted-<key>-1.md.
            prefix = ""
            if path.exists():
                prev = path.read_text(encoding="utf-8", errors="replace")
                pm = _FM_RE.match(prev)
                prefix = (pm.group(2).strip() if pm else prev.strip()) + "\n\n---\n\n"
            else:
                prefix = f"# {title}\n\n"
            fm = (
                "---\n"
                f"title: {title}\n"
                "kind: compacted\n"
                f"tags: {', '.join(all_tags)}\n"
                f"source: compacted:{stem}\n"
                f"created: {created}\n"
                f"count: {len(items)}\n"
                "---\n\n"
            )
            for d in items:
                try:
                    shutil.move(str(d["_path"]), str(archive / d["file"]))
                    moved += 1
                except Exception:  # noqa: BLE001
                    pass
            path.write_text(fm + prefix + merged + "\n", encoding="utf-8")
            out_files.append(path.name)
            doc = _parse(path)
            _ingest_unit(title=doc["title"], body=doc["body"], kind="compacted",
                         tags=all_tags, source=f"compacted:{stem}", repo="notes")

    return {
        "ok": True, "dry_run": False, "group_by": group_by,
        "groups": {k: len(v) for k, v in sorted(planned.items())},
        "files_in": moved, "files_out": len(out_files),
        "compacted": out_files, "archive": str(archive),
    }


def delete_file(name: str) -> bool:
    p = memory_dir() / (name if name.endswith(".md") else f"{name}.md")
    if p.is_file() and p.parent == memory_dir():
        p.unlink()
        return True
    return False
