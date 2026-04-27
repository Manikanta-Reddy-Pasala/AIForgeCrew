"""A/B compare: Cursor-style vs Aider-style retrieval.

Two approaches, same input. Print top-K from each. Operator picks the
winner per query OR computes precision@K against a known answer set.

Cursor approach (vector + cross-encoder reranker):
    raw_text → bge-m3 embed → Neo4j :Symbol vector index top-30 →
    bge-reranker-v2-m3 → top-K

Aider approach (PageRank + personalisation):
    raw_text → mentioned_idents (re.split \\W+) + mentioned_fnames
    (basename match) → RepoMap.get_repo_map → ranked tag digest →
    parse out top-K file paths

Combined view shows side-by-side ranking + which files appear in
both vs only one.

Public:
    compare(text, repo, top_k=8) -> ComparisonResult
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class ComparisonResult:
    cursor: list[str]              # top-K paths from cursor approach
    aider: list[str]               # top-K paths from aider approach
    cursor_ms: int = 0
    aider_ms: int = 0
    overlap: list[str] = field(default_factory=list)   # appear in both
    cursor_only: list[str] = field(default_factory=list)
    aider_only: list[str] = field(default_factory=list)


def compare(text: str, repo: str, *, top_k: int = 8) -> ComparisonResult:
    base = os.environ.get("AIFORGE_REPOS_BASE", "/home/mani/codeRepo")
    worktree = os.path.join(base, repo)

    t0 = time.time()
    cursor_paths = _cursor_retrieval(text, worktree, top_k=top_k)
    cursor_ms = int((time.time() - t0) * 1000)

    t0 = time.time()
    aider_paths = _aider_retrieval(text, worktree, top_k=top_k)
    aider_ms = int((time.time() - t0) * 1000)

    cset, aset = set(cursor_paths), set(aider_paths)
    return ComparisonResult(
        cursor=cursor_paths,
        aider=aider_paths,
        cursor_ms=cursor_ms,
        aider_ms=aider_ms,
        overlap=sorted(cset & aset),
        cursor_only=[p for p in cursor_paths if p not in aset],
        aider_only=[p for p in aider_paths if p not in cset],
    )


def _cursor_retrieval(text: str, worktree: str, *, top_k: int) -> list[str]:
    """Embed → Neo4j :Symbol vector index → cross-encoder rerank."""
    if not worktree or not os.path.isdir(worktree):
        return []
    try:
        from aiforge_core.legacy.embed import embed
        from neo4j import GraphDatabase                # type: ignore
    except Exception:
        return []
    try:
        qvec = embed(text[:1500])
    except Exception:
        return []
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    cy = (
        "CALL db.index.vector.queryNodes('symbol_embedding_vec', $k, $vec) "
        "YIELD node, score "
        "RETURN coalesce(node.file_path, node.file, '') AS path, "
        "       coalesce(node.simple, node.fqn, '')[..200] AS name, "
        "       score ORDER BY score DESC"
    )
    rows = []
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw))
        with drv.session() as s:
            rows = s.run(cy, k=top_k * 4, vec=qvec).data()
        drv.close()
    except Exception:
        return []
    # Optional rerank pass on the symbol-name texts.
    rerank_url = os.environ.get(
        "AIFORGE_RERANK_URL", "http://127.0.0.1:8765",
    )
    if rerank_url and os.environ.get("AIFORGE_RERANK_DISABLE", "0") != "1":
        try:
            import json
            import urllib.request
            texts = [r["name"] or "" for r in rows]
            body = json.dumps({"query": text[:512], "texts": texts}).encode()
            req = urllib.request.Request(
                rerank_url.rstrip("/") + "/rerank",
                data=body, headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                resp = json.loads(r.read())
            scores = resp.get("scores") or []
            for r_, s in zip(rows, scores):
                r_["score"] = 0.7 * float(s) + 0.3 * float(r_["score"])
            rows.sort(key=lambda r: -float(r["score"]))
        except Exception:
            pass
    paths: list[str] = []
    seen: set[str] = set()
    for r in rows:
        p = r.get("path") or ""
        if "src/main/" in p:
            p = p[p.index("src/main/"):]
        elif "src/test/" in p:
            p = p[p.index("src/test/"):]
        if not p or p in seen:
            continue
        seen.add(p)
        full = os.path.join(worktree, p)
        if os.path.isfile(full):
            paths.append(full)
        if len(paths) >= top_k:
            break
    return paths


def _aider_retrieval(text: str, worktree: str, *, top_k: int) -> list[str]:
    """Aider RepoMap with mentioned_idents/mentioned_fnames personalisation.
    Parse the ranked digest for file paths in encounter order."""
    if not worktree or not os.path.isdir(worktree):
        return []
    try:
        from aiforge_core.memory.code_context import aider_digest
    except Exception:
        return []
    digest = aider_digest(
        worktree, chat_files=[], token_budget=2048, user_text=text,
    )
    if not digest:
        return []
    # Aider digest format starts each file section with the relative
    # path on its own line followed by ':' or directly indented tags.
    paths: list[str] = []
    seen: set[str] = set()
    src_re = re.compile(
        r"^([\w./_-]+\.(?:java|py|ts|tsx|js|kt|go))(?::|$)",
        re.MULTILINE,
    )
    for m in src_re.finditer(digest):
        p = m.group(1)
        if p in seen:
            continue
        seen.add(p)
        full = os.path.join(worktree, p)
        if os.path.isfile(full):
            paths.append(full)
        if len(paths) >= top_k:
            break
    return paths


def render_comparison(result: ComparisonResult) -> str:
    """Pretty side-by-side."""
    lines = []
    lines.append(
        f"=== Cursor (vector+rerank, {result.cursor_ms}ms) ===")
    for i, p in enumerate(result.cursor, 1):
        marker = "✓" if p in result.overlap else " "
        lines.append(f"  {i:2d}. {marker} {os.path.basename(p)}  ({p})")
    lines.append("")
    lines.append(
        f"=== Aider (PageRank+mentions, {result.aider_ms}ms) ===")
    for i, p in enumerate(result.aider, 1):
        marker = "✓" if p in result.overlap else " "
        lines.append(f"  {i:2d}. {marker} {os.path.basename(p)}  ({p})")
    lines.append("")
    lines.append(
        f"overlap: {len(result.overlap)}/{len(result.cursor) or 1} "
        f"cursor, {len(result.overlap)}/{len(result.aider) or 1} aider"
    )
    if result.cursor_only:
        lines.append(f"cursor-only: {len(result.cursor_only)}")
        for p in result.cursor_only:
            lines.append(f"  - {os.path.basename(p)}")
    if result.aider_only:
        lines.append(f"aider-only: {len(result.aider_only)}")
        for p in result.aider_only:
            lines.append(f"  - {os.path.basename(p)}")
    return "\n".join(lines)


__all__ = ["compare", "render_comparison", "ComparisonResult"]
