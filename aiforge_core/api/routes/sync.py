"""Hub sync routes (``/api/memory/sync/*``) — the admin's whole surface.

Two directions, four routes:

* ``GET  /manifest`` and ``GET /blob/{digest}`` — what a spoke may pull. Only
  what this machine distilled is advertised (``inbox.downstream``), and a blob
  is served only when its hash is in that same list, so a spoke's raw notes
  cannot be read back out of the admin by anybody who learns a digest.
* ``POST /offer`` and ``POST /push`` — what a spoke may write. ``inbox`` owns
  every acceptance rule; these routes only bound the request and hand it over.

**No credential is required on any of them.** That is deliberate for this
deployment (see ``inbox`` and ``api._sync_open``): the admin is expected to sit
on a trusted interface — a LAN or a WireGuard address — and the control plane,
which runs shells, keeps its token regardless. The bound below is what stands in
for auth: a body larger than the protocol can legitimately produce is refused
before it is read, so an open endpoint cannot be turned into a memory bomb.
"""
from __future__ import annotations

import base64

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

router = APIRouter()

# Largest request body either POST route will read. The push payload is one
# blob, base64-encoded (+33%) inside a small JSON envelope; the offer is a
# manifest, which ``transport`` already caps at 4 MiB on the wire. One number
# for both, big enough for the larger shape.
MAX_BODY_BYTES = 12 * 1024 * 1024


class OfferIn(BaseModel):
    peer: str = ""
    entries: list[dict] = []


class PushIn(BaseModel):
    peer: str = ""
    entry: dict = {}
    body: str = ""          # base64


async def _guard(request: Request) -> None:
    """Refuse an oversized body before Starlette buffers it into memory.

    ``content-length`` is a claim, so this is a cheap early exit rather than the
    enforcement: the real bound is ``inbox.MAX_BLOB_BYTES`` on the decoded bytes
    and ``inbox.MAX_OFFER_ENTRIES`` on the parsed rows. Both are needed — a
    chunked request declares no length at all.
    """
    try:
        declared = int(request.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > MAX_BODY_BYTES:
        raise HTTPException(413, f"body over {MAX_BODY_BYTES} bytes")


@router.get("/api/memory/sync/manifest")
def sync_manifest() -> dict:
    """What a spoke may pull, plus who we are.

    ``admin`` is how a spoke learns whose fold to trust without the operator
    configuring the same id on every machine (``sync.role.remember_admin_id``).
    """
    from aiforge_core.memory.sync import identity, inbox, role

    return {"manifest": inbox.downstream(), "admin": identity.self_id(),
            "role": role.role()}


@router.get("/api/memory/sync/blob/{digest}")
def sync_blob(digest: str) -> Response:
    from aiforge_core.memory.sync import inbox
    from aiforge_core.memory.sync import manifest as _man

    digest = (digest or "").strip().lower()
    if digest not in {str(e.get("hash") or "") for e in inbox.downstream()}:
        # Not "no such file": a hash we hold but do not advertise is one a spoke
        # has no business reading back — its own raw notes, or another spoke's.
        raise HTTPException(404, f"no blob: {digest}")
    path = _man.path_for_hash(digest)
    if path is None:
        raise HTTPException(404, f"no blob: {digest}")
    return Response(content=path.read_bytes(), media_type="text/markdown")


@router.post("/api/memory/sync/offer")
async def sync_offer(payload: OfferIn, request: Request) -> dict:
    """A spoke offers its manifest; we answer with what we do not hold."""
    await _guard(request)
    from aiforge_core.memory.sync import inbox

    inbox.seen(payload.peer or "")
    return {"want": inbox.wanted(payload.entries or [])}


@router.post("/api/memory/sync/push")
async def sync_push(payload: PushIn, request: Request) -> dict:
    """A spoke sends one record we asked for. ``applied`` says whether it stuck."""
    await _guard(request)
    from aiforge_core.memory.sync import inbox

    try:
        body = base64.b64decode(payload.body or "", validate=True)
    except Exception:  # noqa: BLE001 — undecodable bytes are a refused record
        raise HTTPException(400, "body is not valid base64") from None
    return {"applied": inbox.accept(payload.peer or "", payload.entry or {}, body)}
