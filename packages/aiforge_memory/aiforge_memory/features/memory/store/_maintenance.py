"""Maintenance — forget / soft_forget / restore / list / invalidate."""
from __future__ import annotations

import time

from ._helpers import _ALLOWED_LABELS


# ─── Maintenance ──────────────────────────────────────────────────────

def forget(driver, *, repo: str, node_id: str, label: str) -> dict:
    """Hard-delete a memory node + its edges. ``label`` must be one of
    Decision_v2, Observation_v2, Note_v2, Doc_v2."""
    if label not in _ALLOWED_LABELS:
        raise ValueError(f"unknown memory label: {label}")
    cy = (
        f"MATCH (n:{label} {{id:$id, repo:$repo}}) "
        "WITH n, n.id AS id DETACH DELETE n RETURN id"
    )
    with driver.session() as s:
        row = s.run(cy, id=node_id, repo=repo).single()
    return {"deleted": row["id"] if row else None}


def soft_forget(driver, *, repo: str, node_id: str, label: str) -> dict:
    """Soft-delete a memory node by flagging ``status='deleted'`` +
    stamping ``deleted_at``, instead of the hard ``DETACH DELETE`` that
    ``forget`` performs. The node + edges stay intact so it can be
    ``restore``d, and vanilla recall (which only includes
    ``status IS NULL OR status = 'active'``) drops it. ``label`` must be
    one of Decision_v2, Observation_v2, Note_v2, Doc_v2."""
    if label not in _ALLOWED_LABELS:
        raise ValueError(f"unknown memory label: {label}")
    cy = (
        f"MATCH (n:{label} {{id:$id, repo:$repo}}) "
        "SET n.status = 'deleted', "
        "    n.deleted_at = datetime({epochSeconds: toInteger($now)}), "
        "    n.updated_at = datetime({epochSeconds: toInteger($now)}) "
        "RETURN n.id AS id"
    )
    with driver.session() as s:
        row = s.run(cy, id=node_id, repo=repo, now=time.time()).single()
    return {"soft_deleted": row["id"] if row else None}


def restore(driver, *, repo: str, node_id: str, label: str) -> dict:
    """Reverse a ``soft_forget`` — set ``status='active'`` and clear
    ``deleted_at`` so the node re-enters recall. ``label`` must be one of
    Decision_v2, Observation_v2, Note_v2, Doc_v2."""
    if label not in _ALLOWED_LABELS:
        raise ValueError(f"unknown memory label: {label}")
    cy = (
        f"MATCH (n:{label} {{id:$id, repo:$repo}}) "
        "SET n.status = 'active', "
        "    n.deleted_at = null, "
        "    n.updated_at = datetime({epochSeconds: toInteger($now)}) "
        "RETURN n.id AS id"
    )
    with driver.session() as s:
        row = s.run(cy, id=node_id, repo=repo, now=time.time()).single()
    return {"restored": row["id"] if row else None}


def list_memory(
    driver, *, repo: str, label: str | None = None, limit: int = 50,
) -> list[dict]:
    """Return memory nodes for a repo, newest first."""
    if label and label not in _ALLOWED_LABELS:
        raise ValueError(f"unknown memory label: {label}")
    if label:
        cy = (
            f"MATCH (n:{label} {{repo:$repo}}) "
            "RETURN n.id AS id, labels(n)[0] AS label, "
            "       coalesce(n.title,'') AS title, "
            "       coalesce(n.text, n.body, '') AS text, "
            "       coalesce(n.kind, n.status, '') AS kind, "
            "       toString(n.created_at) AS created_at "
            "ORDER BY n.created_at DESC LIMIT $limit"
        )
    else:
        cy = (
            "MATCH (r:Repo {name:$repo})-[:RECORDS]->(n) "
            "WHERE any(l IN labels(n) WHERE l IN "
            "  ['Decision_v2','Observation_v2','Note_v2','Doc_v2']) "
            "RETURN n.id AS id, [l IN labels(n) WHERE l ENDS WITH '_v2'][0] AS label, "
            "       coalesce(n.title,'') AS title, "
            "       coalesce(n.text, n.body, '') AS text, "
            "       coalesce(n.kind, n.status, '') AS kind, "
            "       toString(n.created_at) AS created_at "
            "ORDER BY n.created_at DESC LIMIT $limit"
        )
    with driver.session() as s:
        return [dict(r) for r in s.run(cy, repo=repo, limit=limit)]


# Bi-temporal, replacement-free invalidation (Zep model): a fact that
# stopped being true with NO superseding fact. Non-destructive — sets
# invalid_at + status so recall drops it, but the node + its history
# survive for time-travel / audit ("valid from X until Y").
_INVALIDATE_OBSERVATION = """
MATCH (o:Observation_v2 {id:$id, repo:$repo})
SET o.status = 'superseded',
    o.invalid_at = datetime({epochSeconds: toInteger($now)}),
    o.invalid_reason = $reason,
    o.updated_at = datetime({epochSeconds: toInteger($now)})
RETURN o.id AS id
"""


def invalidate_observation(
    driver, *, repo: str, node_id: str, reason: str = "",
) -> dict:
    """Time-bound a fact that is no longer true without a replacement.

    Non-destructive (Zep-style): sets ``invalid_at`` + ``status`` so the
    fact drops out of recall, but the node and its ``valid_at``..``invalid_at``
    window survive for audit / time-travel. Use ``supersedes=`` on a new
    upsert instead when there IS a correcting fact."""
    with driver.session() as s:
        row = s.run(_INVALIDATE_OBSERVATION, id=node_id, repo=repo,
                    reason=reason or "", now=time.time()).single()
    return {"id": row["id"] if row else node_id, "invalidated": bool(row)}
