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
