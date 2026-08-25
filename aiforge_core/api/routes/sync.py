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
import json

from fastapi import APIRouter, HTTPException, Request, Response

router = APIRouter()

# Largest request body either POST route will read. The push payload is one
# blob, base64-encoded (+33%) inside a small JSON envelope; the offer is a
# manifest, which ``transport`` already caps at 4 MiB on the wire. One number
# for both, big enough for the larger shape.
MAX_BODY_BYTES = 12 * 1024 * 1024


async def _read_json(request: Request) -> dict:
    """Read and parse a bounded request body.

    The routes take a raw ``Request`` and do this by hand rather than declaring
    a pydantic body model, because FastAPI resolves a body parameter — reading
    and parsing the WHOLE request — before the handler runs. A cap checked
    inside the handler is therefore dead code: the bytes are already buffered
    and already parsed into Python objects by the time it looks. This surface
    takes no credential by default, so the bound has to be real.

    ``content-length`` is refused first when it is honest, and the running total
    is what enforces the cap when it is absent or a lie (a chunked request
    declares no length at all). Neither half is sufficient alone.
    """
    try:
        declared = int(request.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > MAX_BODY_BYTES:
        raise HTTPException(413, f"body over {MAX_BODY_BYTES} bytes")

    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            # Abandoned mid-stream: the rest is never read, let alone kept.
            raise HTTPException(413, f"body over {MAX_BODY_BYTES} bytes")
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks) or b"{}")
    except ValueError:
        raise HTTPException(400, "body is not valid JSON") from None
    return payload if isinstance(payload, dict) else {}


@router.get("/api/memory/sync/manifest")
def sync_manifest() -> dict:
    """What a spoke may pull, plus who we are.

    ``admin`` is how a spoke learns whose fold to trust without the operator
    configuring the same id on every machine (``sync.role.remember_admin_id``).
    """
    from aiforge_core.memory.sync import identity, inbox, role

    return {"manifest": inbox.downstream(), "admin": identity.self_id(),
            "role": role.role()}


@router.get("/api/memory/sync/blob/{digest}", responses={404: {"description": "Not found"}})
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
async def sync_offer(request: Request) -> dict:
    """A spoke offers its manifest; we answer with what we do not hold."""
    payload = await _read_json(request)
    from aiforge_core.memory.sync import inbox

    entries = payload.get("entries")
    inbox.seen(str(payload.get("peer") or ""))
    return {"want": inbox.wanted(entries if isinstance(entries, list) else [])}


@router.post("/api/memory/sync/push", responses={400: {"description": "Bad request"}})
async def sync_push(request: Request) -> dict:
    """A spoke sends one record we asked for. ``applied`` says whether it stuck."""
    payload = await _read_json(request)
    from aiforge_core.memory.sync import inbox

    try:
        body = base64.b64decode(str(payload.get("body") or ""), validate=True)
    except Exception:  # noqa: BLE001 — undecodable bytes are a refused record
        raise HTTPException(400, "body is not valid base64") from None
    entry = payload.get("entry")
    return {"applied": inbox.accept(str(payload.get("peer") or ""),
                                    entry if isinstance(entry, dict) else {}, body)}
