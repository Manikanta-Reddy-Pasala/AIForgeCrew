"""Cypher writer for cross-repo edges.

(Repo)-[:CALLS_REPO {via, evidence, confidence, created_at}]->(Repo)

Idempotent on (src, dst, via). Re-running updates evidence + confidence
+ updated_at so stale links self-heal as code changes.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiforge_memory.features.link.extract import CrossRepoEdge


_UPSERT_CALLS_REPO = """
MATCH (a:Repo {name: $src})
MATCH (b:Repo {name: $dst})
MERGE (a)-[r:CALLS_REPO {via: $via}]->(b)
ON CREATE SET r.created_at = datetime({epochSeconds: toInteger($now)})
SET r.evidence   = $evidence,
    r.confidence = $confidence,
    r.updated_at = datetime({epochSeconds: toInteger($now)}),
    r.schema_version = 'codemem-v1'
"""


def upsert_calls_repo(driver, *, edge: "CrossRepoEdge") -> None:  # noqa: UP037 — circular import
    with driver.session() as s:
        s.run(
            _UPSERT_CALLS_REPO,
            src=edge.src, dst=edge.dst, via=edge.via,
            evidence=list(edge.evidence),
            confidence=float(edge.confidence),
            now=time.time(),
        ).consume()


def list_edges(driver, *, repo: str | None = None) -> list[dict]:
    """Return CALLS_REPO edges, optionally filtered by participant repo."""
    cy = (
        "MATCH (a:Repo)-[r:CALLS_REPO]->(b:Repo) "
        + ("WHERE a.name = $repo OR b.name = $repo " if repo else "")
        + "RETURN a.name AS src, b.name AS dst, r.via AS via, "
        "       r.confidence AS confidence, "
        "       coalesce(r.evidence,[]) AS evidence, "
        "       toString(r.updated_at) AS updated_at "
        "ORDER BY r.confidence DESC, src, dst"
    )
    with driver.session() as s:
        params = {"repo": repo} if repo else {}
        return [dict(r) for r in s.run(cy, **params)]
