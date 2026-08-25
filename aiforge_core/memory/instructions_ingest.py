"""Ingest agent-instruction markdown files into OKR memory.

A reproducible, COMMITTED path for seeding memory from a project's agent
instruction files (``CLAUDE.md``, ``AGENTS.md``, ``GEMINI.md``,
``.cursorrules`` — configurable). Each file is split into its markdown sections;
every section is captured as a scoped learning via :func:`md_store.capture`
(which runs the scope classifier and folds the fact into its
``compacted-<scope>.md`` brief), then :func:`md_store.compact` distils the
captures into standard OKR briefs. An optional clean wipe runs first so a re-run
is deterministic.

Backend-agnostic: capture/compact/ingest route through whatever memory backend
is active — the standard md + Postgres + Neo4j graph, or the embedded SQLite
store. Nothing here hardcodes a path or a machine — pass the files (or roots to
scan) as arguments.

CLI::

    aiforge-memory-instructions --clear FILE...      # wipe, then ingest files
    aiforge-memory-instructions --root .             # scan a tree for the files
    aiforge-memory-instructions --root . --name AGENTS.md   # custom filename
    aiforge-memory-instructions --root . --no-compact       # capture only
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Section heading: markdown ATX (# … ###). The heading text becomes the topic;
# the lines under it become one captured fact.
_HEADING_RE = re.compile(r"^(#{1,3})[ \t]+(.+?)[ \t]*#*[ \t]*$")
# Dirs never worth scanning.
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build",
              "__pycache__", "target", ".idea", ".gradle", "vendor"}
# Default agent-instruction filenames recognised across tools.
DEFAULT_NAMES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules")
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
    """Best-effort repo name for an instruction file, GENERICALLY (no hardcoding).

    The nearest ancestor holding a ``.git`` names the repo. A file that is not in
    any git repo before reaching ``$HOME`` (e.g. a personal, tool-global
    instruction file) has no repo → global. The scope classifier in
    :func:`md_store.capture` gets the final say (may promote to global)."""
    try:
        path = path.resolve()
    except OSError:
        return None
    home = Path(os.path.expanduser("~")).resolve()
    for parent in path.parents:
        if (parent / ".git").exists():
            return parent.name
        if parent == home:
            return None                          # not in a repo → global
    return path.parent.name or None


def _walk_for_names(base: Path, nameset: set, found: dict) -> None:
    """Recursive scan of one root, skipping heavy build/vendor dirs."""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if fn in nameset:
                p = Path(dirpath) / fn
                found[str(p.resolve())] = p


def discover(roots: list[str], names: tuple[str, ...] = DEFAULT_NAMES) -> list[Path]:
    """Find instruction files (``names``) under each root (recursive, skipping
    heavy build/vendor dirs). De-duplicated, sorted."""
    nameset = set(names)
    found: dict[str, Path] = {}
    for root in roots:
        base = Path(os.path.expanduser(root))
        if base.is_file():
            if base.name in nameset:
                found[str(base.resolve())] = base
        elif base.is_dir():
            _walk_for_names(base, nameset, found)
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


def _existing_files(files: list[str] | None, roots: list[str] | None,
                    names: tuple[str, ...]) -> list[Path]:
    """The instruction files to read: explicit paths plus whatever the roots
    scan finds, de-duped by resolved path."""
    paths: list[Path] = [Path(os.path.expanduser(f)) for f in (files or [])]
    if roots:
        paths += discover(roots, names)
    seen: set[str] = set()
    real: list[Path] = []
    for p in paths:
        rp = str(p.resolve()) if p.exists() else ""
        if rp and rp not in seen and p.is_file():
            seen.add(rp)
            real.append(p)
    return real


def _capture_sections(md_store, path: Path, text: str, out: dict) -> None:
    """Capture each section of one instruction file.

    Scoped by REPO, not per-heading. Passing the heading as ``topic`` would mint
    one compacted-<heading>.md brief per section (dozens of briefs from one file
    = proliferation). The heading is preserved inside the fact text instead, so
    all of a repo's sections fold into its single compacted-<repo>.md brief. The
    scope classifier may still promote a genuinely cross-project fact to the
    global (shared) brief.
    """
    repo = _repo_hint(path)
    for heading, body in _split_sections(text):
        out["sections"] += 1
        fact = f"{heading}\n{body}" if heading else body
        if len(fact.strip()) < _MIN_SECTION_CHARS:
            out["skipped"] += 1
            continue
        try:
            md_store.capture("learning", fact, repo=repo,
                             title=(heading or path.name)[:70],
                             source=f"instructions:{path.name}")
            out["captured"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad section never aborts
            out.setdefault("errors", []).append(f"{path}#{heading}: {exc}")


def _distil_briefs(md_store, out: dict) -> None:
    try:
        md_store.compact(group_by="repo", min_group=1, summarize=True,
                         model_role="learner", archive_sources=False)
        md_store.compact(group_by="topic", min_group=1, summarize=True,
                         model_role="learner", archive_sources=True)
        md_store.ingest_dir()
    except Exception as exc:  # noqa: BLE001
        out.setdefault("errors", []).append(f"compact: {exc}")


def ingest_instruction_files(
    files: list[str] | None = None, *,
    roots: list[str] | None = None,
    names: tuple[str, ...] = DEFAULT_NAMES,
    clear: bool = False,
    compact: bool = True,
) -> dict:
    """Ingest agent-instruction files → captures → distilled OKR briefs.

    ``files`` — explicit paths. ``roots`` — dirs to scan for ``names``. At least
    one must yield a file. ``clear`` wipes memory data first (deterministic
    re-run). ``compact`` (default) distils captures into standard briefs.
    Returns ``{ok, files, sections, captured, skipped, briefs, cleared}``."""
    from aiforge_core.memory import md_store

    real = _existing_files(files, roots, names)
    if not real:
        return {"ok": False, "error": "no instruction files found", "files": 0}
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
        _capture_sections(md_store, path, text, out)
    if compact:
        _distil_briefs(md_store, out)
    try:
        out["briefs"] = len(md_store.iter_briefs())
    except OSError:
        out["briefs"] = None
    out["ok"] = True
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="aiforge-memory-instructions",
        description="Ingest agent-instruction markdown (CLAUDE.md / AGENTS.md / "
                    "GEMINI.md / .cursorrules) into OKR memory (committed path).")
    ap.add_argument("files", nargs="*", help="explicit instruction file paths")
    ap.add_argument("--root", action="append", default=[], dest="roots",
                    help="dir to scan for instruction files (repeatable)")
    ap.add_argument("--name", action="append", default=[], dest="names",
                    help=f"instruction filename to match (repeatable; "
                         f"default: {', '.join(DEFAULT_NAMES)})")
    ap.add_argument("--clear", action="store_true",
                    help="wipe memory data before ingesting")
    ap.add_argument("--no-compact", action="store_true",
                    help="capture only; skip the distil-to-briefs step")
    ns = ap.parse_args(argv)
    if not ns.files and not ns.roots:
        ap.error("give at least one instruction file or --root DIR")
    import json
    res = ingest_instruction_files(
        ns.files or None, roots=ns.roots or None,
        names=tuple(ns.names) or DEFAULT_NAMES,
        clear=ns.clear, compact=not ns.no_compact)
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
