"""Ingest ``CLAUDE.md`` project-instruction files into OKR memory.

A reproducible, COMMITTED path for seeding memory from CLAUDE.md files. Each
file is split into its markdown sections; every section is captured as a scoped
learning via :func:`md_store.capture` (which runs the scope classifier and folds
the fact into its ``compacted-<scope>.md`` brief), then :func:`md_store.compact`
distils the captures into standard OKR briefs. An optional clean wipe (md files +
sqlite index) runs first so a re-run is deterministic.

Nothing here hardcodes a path or a machine — pass the CLAUDE.md files (or roots
to scan) as arguments.

CLI::

    aiforge-memory-claude --clear FILE...        # wipe, then ingest given files
    aiforge-memory-claude --root .               # scan a dir tree for CLAUDE.md
    aiforge-memory-claude --root . --no-compact   # capture only, skip distil
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Section heading: markdown ATX (# … ###). The heading text becomes the topic;
# the lines under it become one captured fact.
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
# Dirs never worth scanning for a CLAUDE.md.
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build",
              "__pycache__", "target", ".idea", ".gradle", "vendor"}
_MIN_SECTION_CHARS = 12


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into ``(heading, body)`` chunks by ATX headings.

    Content before the first heading is returned under heading "" (the file's
    preamble). Empty/whitespace-only bodies are dropped by the caller."""
    out: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            body = "\n".join(buf).strip()
            if body:
                out.append((heading, body))
            heading = m.group(2).strip()
            buf = []
        else:
            buf.append(line)
    body = "\n".join(buf).strip()
    if body:
        out.append((heading, body))
    return out


def _repo_hint(path: Path) -> str | None:
    """Best-effort repo name for a CLAUDE.md, GENERICALLY (no hardcoding).

    The nearest ancestor holding a ``.git`` names the repo. A CLAUDE.md under the
    user's ``~/.claude`` (personal global instructions) has no repo → global.
    Falls back to the parent directory name. The scope classifier in
    :func:`md_store.capture` gets the final say (may promote to global)."""
    try:
        path = path.resolve()
    except OSError:
        return None
    home = Path(os.path.expanduser("~")).resolve()
    if str(path).startswith(str(home / ".claude")):
        return None                              # personal global instructions
    for parent in path.parents:
        if (parent / ".git").exists():
            return parent.name
        if parent == home:
            break
    return path.parent.name or None


def discover(roots: list[str]) -> list[Path]:
    """Find ``CLAUDE.md`` files under each root (recursive, skipping heavy
    build/vendor dirs). De-duplicated, sorted."""
    found: dict[str, Path] = {}
    for root in roots:
        base = Path(os.path.expanduser(root))
        if base.is_file() and base.name == "CLAUDE.md":
            found[str(base.resolve())] = base
            continue
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            if "CLAUDE.md" in filenames:
                p = Path(dirpath) / "CLAUDE.md"
                found[str(p.resolve())] = p
    return [found[k] for k in sorted(found)]


def _wipe_memory() -> dict:
    """Committed clean wipe of memory DATA across whatever backend is active —
    md files + the index stores (SQLite ``sqlite`` OR the standard Neo4j graph:
    ``graph_facts``/``chunks``/``symbols``/``graphify``). Chat sessions and the
    registered sources/config are preserved. Each store soft-fails, so clearing
    all is safe on any backend (SQLite-only or md+Postgres+Neo4j)."""
    from aiforge_core.memory import admin
    stores = [s for s in admin.CLEARABLE if s != "chat"]
    return {s: admin.clear_store(s) for s in stores}


def ingest_claude_files(
    files: list[str] | None = None, *,
    roots: list[str] | None = None,
    clear: bool = False,
    compact: bool = True,
) -> dict:
    """Ingest CLAUDE.md files → captures → distilled OKR briefs.

    ``files`` — explicit CLAUDE.md paths. ``roots`` — dirs to scan for CLAUDE.md.
    At least one must yield a file. ``clear`` wipes md+sqlite first (deterministic
    re-run). ``compact`` (default) distils captures into standard briefs.
    Returns ``{files, sections, captured, skipped, briefs, cleared}``."""
    from aiforge_core.memory import md_store

    paths: list[Path] = [Path(os.path.expanduser(f)) for f in (files or [])]
    if roots:
        paths += discover(roots)
    # de-dup, keep only existing CLAUDE.md files
    seen: set[str] = set()
    real: list[Path] = []
    for p in paths:
        rp = str(p.resolve()) if p.exists() else ""
        if rp and rp not in seen and p.is_file():
            seen.add(rp)
            real.append(p)
    if not real:
        return {"ok": False, "error": "no CLAUDE.md files found", "files": 0}

    out: dict = {"files": len(real), "sections": 0, "captured": 0,
                 "skipped": 0, "cleared": None}
    if clear:
        out["cleared"] = _wipe_memory()

    for path in real:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            out.setdefault("errors", []).append(f"{path}: {exc}")
            continue
        repo = _repo_hint(path)
        for heading, body in _split_sections(text):
            out["sections"] += 1
            fact = f"{heading}\n{body}" if heading else body
            if len(fact.strip()) < _MIN_SECTION_CHARS:
                out["skipped"] += 1
                continue
            topic = heading or path.parent.name
            try:
                md_store.capture(
                    "learning", fact, repo=repo, topic=topic,
                    title=(heading or path.name)[:70],
                    source=f"claude-md:{path.name}")
                out["captured"] += 1
            except Exception as exc:  # noqa: BLE001 — one bad section never aborts
                out.setdefault("errors", []).append(f"{path}#{heading}: {exc}")

    if compact:
        try:
            md_store.compact(group_by="repo", min_group=1, summarize=True,
                             model_role="learner", archive_sources=False)
            md_store.compact(group_by="topic", min_group=1, summarize=True,
                             model_role="learner", archive_sources=True)
            md_store.ingest_dir()
        except Exception as exc:  # noqa: BLE001
            out.setdefault("errors", []).append(f"compact: {exc}")
    try:
        out["briefs"] = len(list(md_store.memory_dir().glob("compacted-*.md")))
    except OSError:
        out["briefs"] = None
    out["ok"] = True
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="aiforge-memory-claude",
        description="Ingest CLAUDE.md files into OKR memory (committed path).")
    ap.add_argument("files", nargs="*", help="explicit CLAUDE.md paths")
    ap.add_argument("--root", action="append", default=[], dest="roots",
                    help="dir to scan for CLAUDE.md (repeatable)")
    ap.add_argument("--clear", action="store_true",
                    help="wipe md files + sqlite index before ingesting")
    ap.add_argument("--no-compact", action="store_true",
                    help="capture only; skip the distil-to-briefs step")
    ns = ap.parse_args(argv)
    if not ns.files and not ns.roots:
        ap.error("give at least one CLAUDE.md file or --root DIR")
    import json
    res = ingest_claude_files(ns.files or None, roots=ns.roots or None,
                              clear=ns.clear, compact=not ns.no_compact)
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
