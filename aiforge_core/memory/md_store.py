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
from pathlib import Path

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def memory_dir() -> Path:
    raw = os.environ.get("AIFORGE_MEMORY_MD_DIR") or "~/.aiforge/memory"
    p = Path(os.path.expanduser(raw))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "note").lower()).strip("-")
    return (s or "note")[:48]


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


def delete_file(name: str) -> bool:
    p = memory_dir() / (name if name.endswith(".md") else f"{name}.md")
    if p.is_file() and p.parent == memory_dir():
        p.unlink()
        return True
    return False
