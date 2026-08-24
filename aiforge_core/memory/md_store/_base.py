"""md_store internals: filesystem layout, path resolution, frontmatter
parsing, module-level locks + the shared logger/regexes. Leaf layer — every
other md_store submodule builds on this and it imports from none of them."""
from __future__ import annotations

import datetime
import logging
import os
import re
import threading
from pathlib import Path


_log = logging.getLogger("aiforge.md_store")
# `[ \t]*` not `\s*`: `\s` MATCHES the newline, so `---\s*\n` could split a
# run of blank lines many ways — the super-linear case. What is actually
# meant is "trailing spaces/tabs on the --- line".
_FM_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n(.*)$", re.DOTALL)

# Serialize read-modify-write on the .md files — two concurrent chat turns
# (or a chat turn + the auto-memory upsert) writing the same source file
# would otherwise lose one update (both read the same raw, both append, last
# write wins). Covers concurrency within this process.
_WRITE_LOCK = threading.Lock()

# Serializes compact() against ITSELF so two concurrent compactions can't read
# the same stale consolidated state and clobber each other. Held for the whole
# compact (incl. the slow LLM summarise) — but it is a DIFFERENT lock from
# _WRITE_LOCK, so a running compaction does NOT block ordinary chat-turn memory
# writes (upsert_section / append_bullet / write); only Phase 2's brief
# archive+write takes _WRITE_LOCK.
_COMPACT_LOCK = threading.Lock()


def memory_dir() -> Path:
    raw = os.environ.get("AIFORGE_MEMORY_MD_DIR") or os.path.join(
        os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge"), "memory")
    p = Path(os.path.expanduser(raw))
    p.mkdir(parents=True, exist_ok=True)
    return p


def briefs_dir() -> Path:
    """Subfolder holding the consolidated OKR briefs (``compacted-<scope>.md``),
    kept OUT of the memory-dir root (like ``memory-archive/``) so the root only
    holds transient per-run captures. Created lazily. ``migrate_briefs_to_folder``
    moves any legacy root-level briefs in here on startup."""
    p = memory_dir() / "compacted"
    p.mkdir(parents=True, exist_ok=True)
    return p


def captures_dir() -> Path:
    """Subfolder holding the raw per-run capture / session-note ``.md`` files,
    kept OUT of the memory-dir root (next to ``compacted/`` and ``archive/``) so
    the root stays clean — only the ``compacted/``/``archive/``/``captures/``
    folders and markers live there. ``migrate_captures_to_folder`` moves any
    legacy root-level captures in here on startup."""
    p = memory_dir() / "captures"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _capture_md_files() -> list[Path]:
    """Every raw capture file — the ``captures/`` subfolder PLUS any legacy
    root-level ``*.md`` not yet migrated (briefs excluded — they're in
    ``compacted/``). De-duplicated by name (captures/ wins)."""
    seen: dict[str, Path] = {}
    for p in captures_dir().glob("*.md"):
        seen[p.name] = p
    for p in memory_dir().glob("*.md"):             # legacy root (pre-migration)
        if not p.name.startswith("compacted-"):
            seen.setdefault(p.name, p)
    return [seen[k] for k in sorted(seen)]


def brief_path(slug: str) -> Path:
    """Path to a brief file (``<memory>/compacted/compacted-<slug>.md``)."""
    return briefs_dir() / f"compacted-{slug}.md"


def iter_briefs() -> list[Path]:
    """Every consolidated brief file, sorted. Reads the ``compacted/`` subfolder
    AND (transitionally) any legacy root-level ``compacted-*.md`` not yet moved."""
    seen: dict[str, Path] = {}
    for p in briefs_dir().glob("compacted-*.md"):
        seen[p.name] = p
    for p in memory_dir().glob("compacted-*.md"):   # legacy root (pre-migration)
        seen.setdefault(p.name, p)
    return [seen[k] for k in sorted(seen)]


def _md_path_for_stem(stem: str) -> Path:
    """Path for a note stem: a brief (``compacted-*``) lives in the ``compacted/``
    subfolder, every other (per-run capture / session note) in ``captures/``.
    A legacy copy still in the root is honoured by :func:`_resolve_md` on read."""
    if stem.startswith("compacted-"):
        return briefs_dir() / f"{stem}.md"
    return captures_dir() / f"{stem}.md"


def _brief_part_paths(base: str) -> list[Path]:
    """Split-part briefs ``compacted-<base>-N.md`` from the briefs folder AND the
    legacy root (pre-migration), de-duplicated by name, sorted."""
    seen: dict[str, Path] = {}
    for p in list(briefs_dir().glob(f"compacted-{base}-*.md")) \
            + list(memory_dir().glob(f"compacted-{base}-*.md")):
        seen.setdefault(p.name, p)
    return [seen[k] for k in sorted(seen)]


def _all_md_files() -> list[Path]:
    """Every md memory file — root-level per-run captures PLUS the briefs in the
    ``compacted/`` subfolder. De-duplicated by resolved path (a legacy brief may
    still sit in the root before migration)."""
    seen: dict[str, Path] = {}
    for p in _capture_md_files() + iter_briefs():
        try:
            seen[str(p.resolve())] = p
        except OSError:
            seen[str(p)] = p
    return list(seen.values())


def migrate_briefs_to_folder() -> dict:
    """Move legacy root-level ``compacted-*.md`` briefs into ``compacted/``.
    Idempotent; never raises. Skips captures (``<slug>-YYYYMMDD-<6hex>.md``)."""
    moved = 0
    bdir = briefs_dir()
    # ROOT-level briefs ONLY — a brief already in bdir must NEVER be touched
    # (iterating iter_briefs() here would compute dest==source and unlink the
    # live brief — a data-loss bug).
    for p in list(memory_dir().glob("compacted-*.md")):
        if p.parent == bdir:
            continue                                # already in the folder — skip
        if _CAPTURE_SIG_RE.search(p.name):
            continue                                # transient capture, not a brief
        dest = bdir / p.name
        try:
            if dest.exists():
                p.unlink()                          # already migrated → drop dup
            else:
                p.rename(dest)
            moved += 1
        except OSError:
            continue
    return {"ok": True, "moved": moved}


def migrate_captures_to_folder() -> dict:
    """Move legacy root-level capture ``.md`` files into ``captures/``. A capture
    is any root ``*.md`` that is NOT a brief (``compacted-*``). Idempotent; never
    raises. Markers (``.session_okr_marker`` — not ``.md``) and the subfolders
    are untouched."""
    moved = 0
    cdir = captures_dir()
    for p in list(memory_dir().glob("*.md")):       # ROOT level only
        if p.parent == cdir:
            continue                                # already in the folder — skip
        if p.name.startswith("compacted-"):
            continue                                # a brief — handled elsewhere
        dest = cdir / p.name
        try:
            if dest.exists():
                p.unlink()                          # already migrated → drop dup
            else:
                p.rename(dest)
            moved += 1
        except OSError:
            continue
    return {"ok": True, "moved": moved}


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
                # OKR-envelope briefs (work_notes) write JSON-quoted scalars
                meta[k.strip()] = v.strip().strip('"')
    return {
        "name": path.stem,
        "file": path.name,
        "title": meta.get("title") or meta.get("name") or path.stem,
        # OKF identity field is `type:`; accept the legacy `kind:` alias too.
        "kind": meta.get("type") or meta.get("kind") or "note",
        "tags": [t.strip() for t in (meta.get("tags") or "").split(",") if t.strip()],
        # OKF `resource:` supersedes the old `source:`; keep both readable.
        "source": meta.get("source") or meta.get("resource") or "manual",
        "created": meta.get("created") or "",
        # repo + topic drive the two compaction axes (project brief / topic note).
        # topic falls back to a `topic:<slug>` tag when not an explicit field.
        "repo": meta.get("repo") or "",
        "topic": meta.get("topic") or "",
        "preview": body[:240],
        "body": body,
        "path": str(path),
    }


def list_files() -> list[dict]:
    """All md memories (newest first), without full body."""
    out = []
    for p in sorted(_all_md_files(), key=lambda x: x.name, reverse=True):
        try:
            d = _parse(p)
            d.pop("body", None)
            d["mtime"] = p.stat().st_mtime
            out.append(d)
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda d: d.get("created") or "", reverse=True)
    return out


def _resolve_md(name: str) -> Path | None:
    """Resolve a memory-file name to its path in the root OR the ``compacted/``
    briefs subfolder. Rejects any name with a path separator (traversal guard)."""
    fn = name if name.endswith(".md") else f"{name}.md"
    if os.path.basename(fn) != fn:
        return None
    # captures/ + briefs first, then the legacy root (pre-migration copies).
    for d in (captures_dir(), briefs_dir(), memory_dir()):
        p = d / fn
        if p.is_file():
            return p
    return None


def read_file(name: str) -> dict | None:
    p = _resolve_md(name)
    return _parse(p) if p else None


def _brief_title(key: str) -> str:
    """Human title for a brief from its scope key: ``deployment`` → "Deployment",
    ``cicd-pipeline`` → "Cicd Pipeline". So search/UI show a real title, not the
    internal ``compacted-<key>`` stem."""
    return (key or "").replace("-", " ").replace("_", " ").strip().title() or key
# ── cross-scope mapping: link related briefs (project ↔ global ↔ topic) ───────
_CAPTURE_SIG_RE = re.compile(r"-\d{8}-[0-9a-f]{6}\.md$")  # per-run capture stamp
def _find_by_source(source: str) -> Path | None:
    """Locate the md file whose frontmatter ``source`` matches (the stable
    key for a session), so repeated runs UPDATE one file instead of
    spawning a new hashed file every time."""
    for p in _all_md_files():
        try:
            if _parse(p).get("source") == source:
                return p
        except Exception:  # noqa: BLE001
            continue
    return None
