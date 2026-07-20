"""Peer sync routes (/api/memory/sync/*) — read-only, pull-only.

These endpoints only ever read. A peer cannot delete, overwrite, or push
anything through them; the puller decides what it wants. Bearer auth is
inherited from the ``/api/`` middleware in ``aiforge_core/api/api.py``, so no
per-route dependency is needed.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response

router = APIRouter()

_af_log = logging.getLogger("aiforge")


@router.get("/api/memory/sync/challenge")
def sync_challenge(nonce: str) -> dict:
    """Prove we hold the shared mesh key over the caller's ``nonce``.

    The shared-key auto-join handshake, and the ONE sync route reachable
    without a credential (see ``api._auth_exempt``): a candidate cannot present
    the key it is trying to prove it has, and we must not send ours to an
    unverified URL, so instead each side answers the other's nonce with an HMAC.
    Returns 404 when no mesh key is configured — a node not running shared-key
    auto-join simply has no proof to give, and says so rather than 401'ing (a
    401 would be indistinguishable from "wrong key" to the prober).
    """
    from aiforge_core.memory.sync import peers as _peers

    proof = _peers.mesh_proof(nonce or "")
    if not proof:
        raise HTTPException(status_code=404, detail="no mesh key configured")
    return {"proof": proof}


@router.get("/api/memory/sync/manifest")
def sync_manifest() -> dict:
    from aiforge_core.memory.sync import manifest as _man
    from aiforge_core.memory.sync import peers as _peers

    return {"manifest": _man.build(), "roster": _peers.roster()}


@router.get("/api/memory/sync/blob/{digest}")
def sync_blob(digest: str) -> Response:
    from aiforge_core.memory.sync import manifest as _man

    path = _man.path_for_hash(digest)
    if path is None:
        raise HTTPException(404, f"no blob: {digest}")
    return Response(content=path.read_bytes(), media_type="text/markdown")
