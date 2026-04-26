"""Symbol-level code embeddings for Neo4j ``:Symbol`` nodes.

KISS: pull every ``:Symbol`` row that lacks ``embedding``, embed
its name + signature + first 800 chars of body via the existing
embed sidecar, write back via SET. Intended to run as cron (alongside
``aiforge memory mine``) and as a one-shot ``aiforge index symbols
<repo>``.

Includes:
- :func:`backfill(repo=None, batch=200)` — batched UPDATE pass
- :func:`find_similar_code(query, top_k=8, repo=None)` — vector NN
  query the model can call as MCP tool

Toggle: ``AIFORGE_SYMBOL_EMBED=0`` to short-circuit (default on).
"""
from __future__ import annotations

import os
from typing import Iterable


def backfill(repo: str | None = None, *, batch: int = 200) -> dict:
    """Embed every ``:Symbol`` that lacks ``embedding``.

    Returns ``{scanned, embedded, skipped, errors}``.
    """
    if os.environ.get("AIFORGE_SYMBOL_EMBED", "1") != "1":
        return {"scanned": 0, "embedded": 0, "skipped": 0,
                "errors": ["disabled"]}
    try:
        from aiforge_core.legacy.rag.neo4j_memory import _get_driver
    except ImportError:
        return {"scanned": 0, "embedded": 0, "skipped": 0,
                "errors": ["neo4j unavailable"]}
    try:
        from aiforge_core.legacy.embed import embed
    except ImportError:
        return {"scanned": 0, "embedded": 0, "skipped": 0,
                "errors": ["embed sidecar unavailable"]}

    cy_select = (
        "MATCH (s:Symbol) "
        + ("WHERE s.repo = $repo AND s.embedding IS NULL "
           if repo else "WHERE s.embedding IS NULL ")
        + "RETURN s.qname AS qname, s.kind AS kind, s.body AS body, "
        "       s.signature AS signature "
        "LIMIT $batch"
    )
    cy_update = (
        "MATCH (s:Symbol {qname: $qname}) "
        "SET s.embedding = $vec, s.embedded_at = datetime() "
        "RETURN s.qname"
    )

    out = {"scanned": 0, "embedded": 0, "skipped": 0, "errors": []}
    try:
        with _get_driver().session() as sess:
            rows = list(sess.run(cy_select, repo=repo, batch=batch))
            out["scanned"] = len(rows)
            for r in rows:
                qname = r["qname"]
                text = _build_text(qname, r.get("kind"),
                                   r.get("signature"), r.get("body"))
                if not text:
                    out["skipped"] += 1
                    continue
                try:
                    vec = list(embed(text))
                except Exception as exc:
                    out["errors"].append(f"embed {qname}: {exc}")
                    continue
                sess.run(cy_update, qname=qname, vec=vec).consume()
                out["embedded"] += 1
    except Exception as exc:
        out["errors"].append(f"neo4j: {exc}")
    return out


def find_similar_code(
    query: str, *, top_k: int = 8, repo: str | None = None,
) -> list[dict]:
    """Vector NN search over ``:Symbol.embedding``.

    Returns ranked list of ``{qname, repo, kind, signature, score}``.
    Falls back to keyword search via ``s.qname CONTAINS`` when the
    Neo4j vector index is missing.
    """
    if not query.strip():
        return []
    try:
        from aiforge_core.legacy.rag.neo4j_memory import _get_driver
        from aiforge_core.legacy.embed import embed
    except ImportError:
        return []
    try:
        qvec = list(embed(query[:2000]))
    except Exception:
        return _fallback_keyword(query, top_k=top_k, repo=repo)

    cy = (
        "CALL db.index.vector.queryNodes('symbol_embedding', $k, $vec) "
        "YIELD node, score "
        + ("WHERE node.repo = $repo " if repo else "")
        + "RETURN node.qname AS qname, node.repo AS repo, "
        "       node.kind AS kind, node.signature AS signature, score "
        "ORDER BY score DESC"
    )
    try:
        with _get_driver().session() as sess:
            return [dict(r) for r in sess.run(
                cy, k=top_k, vec=qvec, repo=repo,
            )]
    except Exception:
        return _fallback_keyword(query, top_k=top_k, repo=repo)


def _fallback_keyword(
    query: str, *, top_k: int, repo: str | None,
) -> list[dict]:
    try:
        from aiforge_core.legacy.rag.neo4j_memory import _get_driver
    except ImportError:
        return []
    cy = (
        "MATCH (s:Symbol) "
        "WHERE toLower(s.qname) CONTAINS toLower($q) "
        + ("AND s.repo = $repo " if repo else "")
        + "RETURN s.qname AS qname, s.repo AS repo, s.kind AS kind, "
        "       s.signature AS signature, 0.5 AS score "
        "LIMIT $k"
    )
    try:
        with _get_driver().session() as sess:
            return [dict(r) for r in sess.run(
                cy, q=query, k=top_k, repo=repo,
            )]
    except Exception:
        return []


def _build_text(qname, kind, signature, body) -> str:
    parts = [str(qname or "")]
    if kind:
        parts.append(f"({kind})")
    if signature:
        parts.append(str(signature))
    if body:
        parts.append(str(body)[:800])
    return " ".join(p for p in parts if p).strip()
