"""Code-context fetchers for the Doer prompt.

Two complementary sources, both injected into the Doer's user_input:

1. **Aider RepoMap** — local in-process call to ``aider.repomap.RepoMap``.
   Tree-sitter PageRank-ranked tag digest. Hot path. Token-budgeted.

2. **Graphify-derived neighbour symbols** — Cypher query against the
   ``:File`` / ``:Symbol`` graph populated by ``graphify_loader`` and
   ``treesitter_ingest``. Pulls inferred semantic edges (Graphify's
   INFERRED CALLS) that tree-sitter alone misses.

Both sources are best-effort. If the lib or the graph is unavailable,
the function returns "" and the Doer falls back to ticket body only.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


# ─────────────── Aider RepoMap (process-local, hot path) ────────────────
def aider_digest(worktree: str, chat_files: list[str],
                 token_budget: int = 1024) -> str:
    """Run Aider's RepoMap on the worktree and return its ranked digest.

    Returns "" on any error (Aider not installed, repo too small, etc).
    Caller injects the result verbatim into the Doer system prompt.
    """
    if os.environ.get("AIFORGE_AIDER_REPOMAP_ENABLED", "1") != "1":
        return ""
    try:
        from aiforge_core.index.aider_map import (
            AiderMapConfig, render_repo_map,
        )
    except Exception:
        return ""
    root = Path(worktree)
    other = _enumerate_repo_files(root, exclude=set(chat_files))
    cfg = AiderMapConfig(
        root=root,
        chat_files=chat_files,
        other_files=other,
        map_tokens=token_budget,
    )
    try:
        digest = render_repo_map(cfg) or ""
    except Exception:
        return ""
    return digest


_REPO_MAP_EXTS = (".java", ".py", ".ts", ".tsx", ".js", ".kt", ".go")
_REPO_MAP_EXCLUDE = {
    ".git", ".aider.tags.cache.v4", "target", "build",
    "node_modules", ".idea", ".vscode", "__pycache__",
}


def _enumerate_repo_files(root: Path, exclude: set[str],
                          cap: int = 4000) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _REPO_MAP_EXCLUDE]
        for f in filenames:
            if not f.endswith(_REPO_MAP_EXTS):
                continue
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root)
            if rel in exclude:
                continue
            out.append(full)
            if len(out) >= cap:
                return out
    return out


# ──────── Graphify / tree-sitter neighbour symbols (graph hop) ─────────
# Match by suffix — the indexer stores absolute paths from whatever
# host did the scan (Mac Studio: `/Users/manikanta/...`; NUC reindex:
# `/home/mani/...`). Doer passes either rel or abs paths, so we use
# ENDS WITH on the repo-relative tail.
#
# Rel names come from `treesitter_ingest` (CALLS, DEFINES) and the
# Graphify mirror (additional CALLS/IMPORTS). DEFINES_METHOD doesn't
# exist; checked actual rel inventory before listing here.
_NEIGHBOURS_CYPHER = """
UNWIND $paths AS path
OPTIONAL MATCH (s:Symbol)
WHERE s.file_path ENDS WITH path
WITH collect(DISTINCT s) AS syms
UNWIND syms AS center
OPTIONAL MATCH (center)-[r:CALLS|IMPORTS|EXTENDS|IMPLEMENTS|DEFINES]-(other:Symbol)
WHERE other <> center
RETURN DISTINCT
  center.fqn AS from_fqn,
  type(r) AS rel,
  other.fqn AS to_fqn,
  other.kind AS kind,
  coalesce(other.return_type, '') + ' ' + coalesce(other.simple, '') AS sig
LIMIT $limit
"""


def graph_neighbours(file_paths: list[str], limit: int = 30) -> str:
    """Return a short text block of symbols linked to the in-scope files.

    Pulls calls/imports/extends/implements edges from Neo4j (populated
    by tree-sitter ingest + Graphify mirror). Filters to ≤30 lines so
    the doer prompt doesn't explode.

    Returns "" on driver error or empty results.
    """
    if not file_paths:
        return ""
    if os.environ.get("AIFORGE_DOER_GRAPH_NEIGHBOURS", "1") != "1":
        return ""
    try:
        from neo4j import GraphDatabase  # type: ignore
    except Exception:
        return ""
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    # Reduce to the repo-relative tail. The indexer keeps the host-side
    # absolute path; matching by ENDS WITH on the relative suffix is
    # both fast (suffix index for Symbol.file_path is implicit on small
    # corpora) and host-agnostic.
    suffix_paths: set[str] = set()
    for p in file_paths:
        # Drop everything before "src/main/" or first repo marker.
        for marker in ("src/main/", "src/test/"):
            idx = p.find(marker)
            if idx >= 0:
                suffix_paths.add(p[idx:])
                break
        else:
            # Fall back to the relative path as-is.
            suffix_paths.add(p)
    rel_paths = list(suffix_paths)[:20]
    rows: list[dict] = []
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw))
        with drv.session() as s:
            rows = s.run(_NEIGHBOURS_CYPHER, paths=rel_paths, limit=limit).data()
        drv.close()
    except Exception:
        return ""
    if not rows:
        return ""
    lines = [f"[Graphify+tree-sitter — top-{len(rows)} neighbour symbols]"]
    for r in rows:
        if not r.get("from_fqn") or not r.get("to_fqn"):
            continue
        sig = (r.get("sig") or "").strip().replace("\n", " ")[:80]
        lines.append(
            f"- {r['from_fqn']} --{r['rel']}--> {r['to_fqn']}"
            + (f"  ({sig})" if sig else "")
        )
    return "\n".join(lines) if len(lines) > 1 else ""
