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
import time
from pathlib import Path
from typing import Iterable


def should_refresh(
    changed_paths: list[str],
    *,
    exts: tuple[str, ...] = (".py", ".java", ".ts", ".go", ".rs"),
) -> bool:
    """Return True when any changed path is a code file worth re-embedding
    (gap A8b). Pure predicate — no I/O."""
    return any(
        str(p).lower().endswith(exts) for p in (changed_paths or [])
    )


def request_refresh(changed_paths: list[str]) -> dict:
    """Push-on-change trigger for symbol embedding (gap A8b).

    When ``AIFORGE_SYMBOL_PUSH_REFRESH=1`` and ``changed_paths`` contains
    code files, touch a sentinel under ``~/.aiforge`` so the existing
    embed cron can pick the change up early instead of waiting for its
    15-min tick. Additive + no-op-safe: returns
    ``{requested: bool, marker: str|None}`` and never raises on the
    happy path.
    """
    if os.environ.get("AIFORGE_SYMBOL_PUSH_REFRESH") != "1":
        return {"requested": False, "marker": None}
    if not should_refresh(changed_paths):
        return {"requested": False, "marker": None}
    marker = Path.home() / ".aiforge" / "symbol_refresh.request"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        return {"requested": False, "marker": None, "error": str(exc)[:80]}
    return {"requested": True, "marker": str(marker)}


def consume_refresh_marker() -> bool:
    """True (and clears the sentinel) if a push-refresh was requested
    since the last cron tick. Lets the existing embed cron run early
    on change. No-op-safe."""
    marker = Path.home() / ".aiforge" / "symbol_refresh.request"
    try:
        if marker.is_file():
            marker.unlink()
            return True
    except OSError:
        return False
    return False


def backfill(repo: str | None = None, *, batch: int = 200) -> dict:
    """Embed every ``:Symbol`` that lacks ``embedding``.

    Returns ``{scanned, embedded, skipped, errors}``.
    """
    if os.environ.get("AIFORGE_SYMBOL_EMBED", "1") != "1":
        return {"scanned": 0, "embedded": 0, "skipped": 0,
                "errors": ["disabled"]}
    try:
        from aiforge_core.memory.rag.neo4j_memory import _get_driver
    except ImportError:
        return {"scanned": 0, "embedded": 0, "skipped": 0,
                "errors": ["neo4j unavailable"]}
    try:
        from aiforge_core.memory.embed import embed
    except ImportError:
        return {"scanned": 0, "embedded": 0, "skipped": 0,
                "errors": ["embed sidecar unavailable"]}

    # AIForge :Symbol schema (graphify + tree-sitter): fqn, simple,
    # kind, repo, file_path, return_type, param_types, modifiers,
    # start_line, end_line. No 'body' / 'signature' fields — we
    # synthesise both from the available scalars + (optional) on-disk
    # slice between start_line/end_line.
    cy_select = (
        "MATCH (s:Symbol) "
        + ("WHERE s.repo = $repo AND s.embedding IS NULL "
           if repo else "WHERE s.embedding IS NULL ")
        + "RETURN s.fqn AS fqn, s.simple AS simple, s.kind AS kind, "
        "       s.return_type AS return_type, s.param_types AS param_types, "
        "       s.repo AS repo, s.file_path AS file_path, "
        "       s.start_line AS start_line, s.end_line AS end_line "
        "LIMIT $batch"
    )
    cy_update = (
        "MATCH (s:Symbol {fqn: $fqn}) "
        "SET s.embedding = $vec, s.embedded_at = datetime() "
        "RETURN s.fqn"
    )

    out = {"scanned": 0, "embedded": 0, "skipped": 0, "errors": []}
    try:
        with _get_driver().session() as sess:
            rows = list(sess.run(cy_select, repo=repo, batch=batch))
            out["scanned"] = len(rows)
            for r in rows:
                fqn = r["fqn"]
                text = _build_text(
                    fqn,
                    simple=r.get("simple"),
                    kind=r.get("kind"),
                    return_type=r.get("return_type"),
                    param_types=r.get("param_types"),
                    file_path=r.get("file_path"),
                    start_line=r.get("start_line"),
                    end_line=r.get("end_line"),
                )
                if not text:
                    out["skipped"] += 1
                    continue
                try:
                    vec = list(embed(text))
                except Exception as exc:
                    out["errors"].append(f"embed {fqn}: {str(exc)[:80]}")
                    if len(out["errors"]) >= 5:
                        # Sidecar likely down — bail early instead of
                        # logging 200× the same connection-refused.
                        out["errors"].append("...truncated, sidecar down")
                        break
                    continue
                sess.run(cy_update, fqn=fqn, vec=vec).consume()
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
        from aiforge_core.memory.rag.neo4j_memory import _get_driver
        from aiforge_core.memory.embed import embed
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
        + "RETURN node.fqn AS fqn, node.repo AS repo, "
        "       node.kind AS kind, node.return_type AS return_type, "
        "       node.file_path AS file_path, node.start_line AS line, "
        "       score "
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
        from aiforge_core.memory.rag.neo4j_memory import _get_driver
    except ImportError:
        return []
    cy = (
        "MATCH (s:Symbol) "
        "WHERE toLower(s.fqn) CONTAINS toLower($q) "
        + ("AND s.repo = $repo " if repo else "")
        + "RETURN s.fqn AS fqn, s.repo AS repo, s.kind AS kind, "
        "       s.return_type AS return_type, s.file_path AS file_path, "
        "       s.start_line AS line, 0.5 AS score "
        "LIMIT $k"
    )
    try:
        with _get_driver().session() as sess:
            return [dict(r) for r in sess.run(
                cy, q=query, k=top_k, repo=repo,
            )]
    except Exception:
        return []


def _build_text(
    fqn, *, simple=None, kind=None, return_type=None,
    param_types=None, file_path=None,
    start_line=None, end_line=None,
) -> str:
    """Synthesise embedding text from available :Symbol scalars +
    optional on-disk source slice. KISS: when start_line/end_line
    point at a real file, splice the body in. Capped at 800 chars
    so embedding cost stays bounded."""
    parts: list[str] = [str(fqn or simple or "")]
    if kind:
        parts.append(f"({kind})")
    if return_type:
        parts.append(f"-> {return_type}")
    if param_types:
        parts.append(f"({', '.join(map(str, param_types))})")
    body = ""
    if file_path and start_line:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            lo = max(0, int(start_line) - 1)
            hi = min(len(lines), int(end_line or start_line) + 1)
            body = "".join(lines[lo:hi])[:800]
        except Exception:
            body = ""
    if body:
        parts.append(body)
    return " ".join(p for p in parts if p).strip()
