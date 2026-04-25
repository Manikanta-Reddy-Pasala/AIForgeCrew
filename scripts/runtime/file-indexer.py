#!/usr/bin/env python3
"""File-hash indexer for ~/codeRepo/*.

Walks every target repo, computes SHA256 per file, compares to the hash
stored in memories.metadata.file_hash (wing=code/<repo>, source=<rel>).
If new or changed:
  - Reads first 8000 chars.
  - Extracts a compact outline: lang, classes, functions, imports.
  - Upserts a single `t4` memory row with:
      text    = the compact outline summary
      wing    = code/<repo>
      source  = <repo-relative-path>
      metadata= {file_hash, size, mtime, lang, outline_ver}

If a stored entry exists but the file is gone, the wing is rewritten to
`code/<repo>/archived` so search still finds it (for historical lookups)
but freshness filters skip it.

Schedule: launchd StartInterval 1800s (30 min). Idempotent — hash
comparison means unchanged files cost one stat + one hash computation.

Run manually:  .venv/bin/python scripts/runtime/file-indexer.py [--full]
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterator

import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aiforge_core.runtime.config import AIFORGE_DSN, WORKTREE_ROOT
from aiforge_core.legacy.store_v2 import Store

log = logging.getLogger("file-indexer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


OUTLINE_VER = 1  # bump when outline extraction logic changes → forces re-summary

# Repos to skip (orchestrator source, build artifacts, venvs).
SKIP_REPOS = {"AIForgeCrew"}

# Extensions we index (source code + config + docs).
INDEXABLE_EXT = {
    ".java", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
    ".kt", ".scala", ".rb", ".php", ".cs", ".cpp", ".c", ".h",
    ".yaml", ".yml", ".xml", ".toml", ".sh", ".sql",
    ".md", ".txt",
}

# Directories to skip inside each repo.
SKIP_DIRS = {
    "node_modules", ".git", "target", "dist", "build", "__pycache__",
    ".venv", "venv", ".gradle", ".idea", ".vscode", ".mvn",
    "out", "bin", "obj", ".next", ".nuxt", "coverage",
    ".aiforge-worktrees",  # per-ticket worktrees; indexed via their repo
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
}

MAX_FILE_BYTES = 1_000_000   # 1 MB — skip bigger
MAX_READ_BYTES = 8_192       # chars fed to outline extractor


# ─────────────────────────── outline extraction ────────────────────────
_CLASS_PATTERNS = {
    ".java":   re.compile(r"^\s*(?:public|private|protected|abstract|final|static|\s)*\s*(?:class|interface|enum|record)\s+(\w+)", re.M),
    ".py":     re.compile(r"^class\s+(\w+)", re.M),
    ".ts":     re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", re.M),
    ".tsx":    re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", re.M),
    ".js":     re.compile(r"^\s*(?:export\s+)?class\s+(\w+)", re.M),
    ".go":     re.compile(r"^type\s+(\w+)\s+struct", re.M),
    ".kt":     re.compile(r"^\s*(?:open\s+|abstract\s+|sealed\s+|data\s+)?class\s+(\w+)", re.M),
}

_FUNC_PATTERNS = {
    ".java":   re.compile(r"^\s*(?:public|private|protected|static|final|synchronized|\s)*\s*(?:[\w<>\[\],.?]+\s+)+(\w+)\s*\([^)]*\)\s*\{", re.M),
    ".py":     re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\(", re.M),
    ".ts":     re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)|^\s*(?:public|private|protected)?\s*(?:async\s+)?(\w+)\s*\([^)]*\)[:\s]*\{", re.M),
    ".tsx":    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", re.M),
    ".js":     re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", re.M),
    ".go":     re.compile(r"^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(", re.M),
    ".kt":     re.compile(r"^\s*(?:override\s+|suspend\s+)?fun\s+(\w+)\s*\(", re.M),
}


def _lang_for(ext: str) -> str:
    return {
        ".java": "java", ".py": "python", ".ts": "typescript",
        ".tsx": "tsx", ".js": "javascript", ".jsx": "jsx",
        ".go": "go", ".rs": "rust", ".kt": "kotlin",
        ".md": "markdown", ".sql": "sql", ".sh": "bash",
        ".yaml": "yaml", ".yml": "yaml", ".xml": "xml",
        ".toml": "toml", ".c": "c", ".cpp": "cpp", ".h": "c-header",
    }.get(ext, "text")


def extract_outline_parts(rel_path: str, ext: str, content: str) -> dict:
    """Return structured outline pieces so both per-file and per-feature
    summaries can reuse the same extraction."""
    lang = _lang_for(ext)
    head = ""
    for line in content.splitlines()[:30]:
        s = line.strip().lstrip("/").lstrip("#").lstrip('"').lstrip("*").strip()
        if s and len(s) > 5:
            head = s[:160]
            break
    classes = _CLASS_PATTERNS.get(ext, None)
    funcs = _FUNC_PATTERNS.get(ext, None)
    class_names: list[str] = []
    func_names: list[str] = []
    if classes is not None:
        class_names = sorted(set(classes.findall(content)))[:20]
    if funcs is not None:
        raw = funcs.findall(content)
        flat = [n for grp in raw for n in (grp if isinstance(grp, tuple) else [grp]) if n]
        func_names = sorted(set(flat))[:30]
    return {"rel": rel_path, "lang": lang, "header": head,
            "classes": class_names, "funcs": func_names}


def build_outline(rel_path: str, ext: str, content: str) -> str:
    """Compact, searchable, one-string summary of a file."""
    o = extract_outline_parts(rel_path, ext, content)
    parts = [f"{o['rel']} ({o['lang']})"]
    if o["header"]:
        parts.append(f"header: {o['header']}")
    if o["classes"]:
        parts.append(f"classes: {', '.join(o['classes'])}")
    if o["funcs"]:
        parts.append(f"functions: {', '.join(o['funcs'])}")
    if not o["classes"] and not o["funcs"]:
        parts.append(f"snippet: {content[:200].replace(chr(10), ' ')}")
    return " | ".join(parts)


# ─────────────────────────── repo walker ────────────────────────────
def iter_repo_files(repo_root: Path) -> Iterator[Path]:
    """Yield indexable files under `repo_root`, skipping junk dirs."""
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in INDEXABLE_EXT:
                continue
            p = Path(dirpath) / fn
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size == 0 or size > MAX_FILE_BYTES:
                continue
            yield p


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


# ─────────────────────────── index + upsert ────────────────────────
def existing_index(cur, wing: str) -> dict[str, dict]:
    """Return {source_rel_path: {id, hash, outline_ver, size}} for a wing."""
    cur.execute(
        "SELECT id, source, metadata FROM memories "
        "WHERE tier='t4' AND wing = %s",
        (wing,),
    )
    out: dict[str, dict] = {}
    for row_id, source, metadata in cur.fetchall():
        md = metadata or {}
        out[source or ""] = {
            "id": row_id,
            "hash": md.get("file_hash"),
            "outline_ver": md.get("outline_ver", 0),
            "size": md.get("size", 0),
        }
    return out


def _feature_group(rel_path: str) -> str | None:
    """If the file sits under a `.../feature/<name>/...` tree, return the
    feature directory (relative to repo root). Catches both:
      - src/main/java/com/pos/backend/feature/businessProduct/Foo.java
      - src/main/java/com/oneshell/business/application/feature/products/Bar.java
    Returns None when the file isn't inside a feature folder."""
    parts = rel_path.split("/")
    for i, seg in enumerate(parts):
        if seg == "feature" and i + 1 < len(parts) - 1:
            return "/".join(parts[:i + 2])
    return None


def build_feature_rollup(feature_path: str, files: list[dict]) -> str:
    """One paragraph summary of a feature folder's contents.

    files = list of {rel, lang, classes:[], funcs:[], header}
    """
    name = feature_path.rsplit("/", 1)[-1]
    langs = sorted({f["lang"] for f in files})
    all_classes: set[str] = set()
    all_funcs: set[str] = set()
    controllers: list[str] = []
    services: list[str] = []
    models: list[str] = []
    for f in files:
        all_classes.update(f["classes"])
        all_funcs.update(f["funcs"])
        for c in f["classes"]:
            low = c.lower()
            if "controller" in low:
                controllers.append(c)
            elif "service" in low:
                services.append(c)
            elif "dao" in low or "entity" in low or low.endswith(("dao", "dto", "request", "response")):
                models.append(c)
    parts = [f"feature={name} (files={len(files)}, langs={','.join(langs)})"]
    if controllers:
        parts.append(f"controllers: {', '.join(sorted(set(controllers))[:10])}")
    if services:
        parts.append(f"services: {', '.join(sorted(set(services))[:10])}")
    if models:
        parts.append(f"models: {', '.join(sorted(set(models))[:10])}")
    file_list = ", ".join(f["rel"].rsplit("/", 1)[-1] for f in files[:12])
    if len(files) > 12:
        file_list += f", ... (+{len(files)-12} more)"
    parts.append(f"files: {file_list}")
    return " | ".join(parts)


def index_repo(store: Store, repo_name: str, repo_root: Path, *, full: bool) -> dict:
    """Index a single repo. Returns stats."""
    wing = f"code/{repo_name}"
    feature_wing = f"feature/{repo_name}"
    stats = {"repo": repo_name, "scanned": 0, "changed": 0,
             "new": 0, "archived": 0, "unchanged": 0, "errors": 0,
             "features": 0}
    seen: set[str] = set()
    # Collected for feature rollup pass.
    per_feature: dict[str, list[dict]] = {}

    with psycopg.connect(AIFORGE_DSN, connect_timeout=5) as conn, conn.cursor() as cur:
        existing = existing_index(cur, wing)

        for p in iter_repo_files(repo_root):
            try:
                rel = str(p.relative_to(repo_root))
            except ValueError:
                continue
            seen.add(rel)
            stats["scanned"] += 1
            try:
                h = sha256_of(p)
            except OSError:
                stats["errors"] += 1
                continue
            prev = existing.get(rel)
            fg = _feature_group(rel)
            unchanged = (prev and prev.get("hash") == h
                         and prev.get("outline_ver") == OUTLINE_VER and not full)
            if unchanged and not fg:
                stats["unchanged"] += 1
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="replace")[:MAX_READ_BYTES]
            except OSError:
                stats["errors"] += 1
                continue
            ext = p.suffix.lower()
            parts = extract_outline_parts(rel, ext, content)
            # Aggregate into feature bucket (even for unchanged files, so
            # rollup can be regenerated without re-embedding outlines).
            if fg:
                per_feature.setdefault(fg, []).append(parts)
            if unchanged:
                stats["unchanged"] += 1
                continue
            outline = build_outline(rel, ext, content)
            mtime = int(p.stat().st_mtime)
            md = {"file_hash": h, "size": p.stat().st_size,
                  "mtime": mtime, "lang": _lang_for(ext),
                  "outline_ver": OUTLINE_VER, "repo": repo_name}
            if prev:
                stats["changed"] += 1
            else:
                stats["new"] += 1
            # upsert_code_chunk handles DELETE-by-source + INSERT + embed.
            store.upsert_code_chunk(repo=repo_name, path=rel,
                                    text=outline, metadata=md)

        # Archive rows for files that no longer exist. Only touch rows
        # this indexer wrote (outline_ver set). Leaves legacy t4 chunks
        # from graphify/upsert_code_chunk alone.
        for src, meta in existing.items():
            if not src or src in seen:
                continue
            if not meta.get("outline_ver"):
                continue
            cur.execute(
                "UPDATE memories SET wing = %s, "
                "  metadata = jsonb_set(COALESCE(metadata,'{}'::jsonb), "
                "    '{archived_at}', to_jsonb(%s::text)) "
                "WHERE id = %s",
                (f"{wing}/archived", time.strftime("%Y-%m-%dT%H:%M:%SZ"), meta["id"]),
            )
            conn.commit()
            stats["archived"] += 1

    # ─── Feature rollup pass ─────────────────────────────────────
    # One aggregate row per `feature/<name>` dir under the repo. Uses
    # the per-feature file list accumulated during the file loop.
    # Purged and rebuilt fully each run — cheap (few hundred rows total).
    if per_feature:
        with psycopg.connect(AIFORGE_DSN, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM memories WHERE tier='t4' AND wing = %s",
                (feature_wing,),
            )
            conn.commit()
        for feature_path, files in sorted(per_feature.items()):
            if not files:
                continue
            text = build_feature_rollup(feature_path, files)
            md = {"feature": feature_path.rsplit("/", 1)[-1],
                  "feature_path": feature_path,
                  "file_count": len(files),
                  "repo": repo_name,
                  "outline_ver": OUTLINE_VER,
                  "rollup": True}
            # Reuse upsert_code_chunk — it sets tier=t4 and wing=code/<repo>.
            # We manually insert to use the `feature/<repo>` wing instead.
            from aiforge_core import embedder as embed_mod
            from aiforge_core.legacy.store_v2 import _vec_literal
            import json as _json
            vec = embed_mod.embed(text)
            with psycopg.connect(AIFORGE_DSN, connect_timeout=5) as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO memories
                       (tier, wing, kind, source, title, text, embedding, metadata)
                       VALUES ('t4', %s, 'feature_rollup', %s, %s, %s, %s::vector, %s::jsonb)""",
                    (feature_wing, feature_path,
                     md["feature"], text, _vec_literal(vec),
                     _json.dumps(md)),
                )
                conn.commit()
            stats["features"] += 1

    return stats


# ─────────────────────────── main ────────────────────────────
def main(full: bool = False, only: str | None = None) -> int:
    t0 = time.time()
    worktree = Path(os.path.expanduser(WORKTREE_ROOT))
    if not worktree.is_dir():
        log.error("WORKTREE_ROOT %s not a dir", worktree)
        return 1
    repos: list[tuple[str, Path]] = []
    for entry in sorted(worktree.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        if entry.name in SKIP_REPOS:
            continue
        if only and entry.name != only:
            continue
        repos.append((entry.name, entry))

    store = Store(AIFORGE_DSN)
    totals = {"scanned": 0, "changed": 0, "new": 0, "archived": 0,
              "unchanged": 0, "errors": 0, "features": 0}
    for name, root in repos:
        try:
            stats = index_repo(store, name, root, full=full)
        except Exception as exc:
            log.exception("index_repo failed for %s: %s", name, exc)
            continue
        log.info("%s: scanned=%d new=%d changed=%d archived=%d unchanged=%d features=%d errors=%d",
                 name, stats["scanned"], stats["new"], stats["changed"],
                 stats["archived"], stats["unchanged"],
                 stats.get("features", 0), stats["errors"])
        for k in totals:
            totals[k] = totals.get(k, 0) + stats.get(k, 0)
    log.info("total: scanned=%d new=%d changed=%d archived=%d unchanged=%d features=%d errors=%d dur=%.1fs",
             totals["scanned"], totals["new"], totals["changed"],
             totals["archived"], totals["unchanged"],
             totals.get("features", 0), totals["errors"],
             time.time() - t0)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="Re-outline every file regardless of hash")
    ap.add_argument("--only", help="Only index this repo name")
    args = ap.parse_args()
    sys.exit(main(full=args.full, only=args.only))
