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
_NEIGHBOURS_CYPHER = """
UNWIND $paths AS path
OPTIONAL MATCH (f:File {path: path})
OPTIONAL MATCH (s:Symbol {file_path: path})
WITH collect(DISTINCT f) AS files, collect(DISTINCT s) AS syms
UNWIND syms AS center
OPTIONAL MATCH (center)-[r:CALLS|IMPORTS|EXTENDS|IMPLEMENTS|DEFINES_METHOD]-(other:Symbol)
WHERE other <> center
WITH center, type(r) AS rel, other
RETURN DISTINCT
  center.fqn AS from_fqn,
  rel,
  other.fqn AS to_fqn,
  other.kind AS kind,
  coalesce(other.signature, '') AS sig
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
    # Strip absolute prefix; Neo4j stores repo-relative paths.
    rel_paths: list[str] = []
    for p in file_paths:
        if "PosClientBackend/" in p:
            rel_paths.append(p[p.index("PosClientBackend/") + len("PosClientBackend/"):])
        else:
            rel_paths.append(p)
    rel_paths = list({*rel_paths, *file_paths})[:20]
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
