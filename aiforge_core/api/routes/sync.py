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
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response

router = APIRouter()

# One admin may serve several independent fleets. The group names which, and it
# rides every call: a query parameter on the two GETs, a body field on the two
# POSTs (where it travels beside the peer id it belongs to).
GroupQ = Annotated[str, Query(description="Group to sync with; omit when ungrouped")]


def _scope(name: str):
    """Enter the caller's group, or refuse the name.

    An unknown group is **404 naming the known ones**, never a silently created
    directory: auto-creation is exactly how a client-side typo becomes a second
    pool that looks like a working sync until somebody asks why two machines
    cannot see each other. An unusable name is 400 — it could not have been a
    directory component in the first place.

    An empty name is the ungrouped deployment and yields a no-op scope, so every
    route below reads the same whether or not this admin has groups.
    """
    from aiforge_core.memory.sync import group

    name = (name or "").strip()
    if not name:
        return group.scoped("")
    if not group.is_valid(name):
        raise HTTPException(400, f"{name!r} is not a usable group name")
    rows = group.known()
    if name not in rows:
        raise HTTPException(
            404, f"no such group: {name}. This admin publishes: "
                 f"{', '.join(rows) or '(none — it is ungrouped)'}")
    return group.scoped(name)

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


@router.get("/api/memory/sync/groups")
def sync_groups() -> dict:
    """What a client may join. Open, like the rest of this surface.

    Discovery is what stops an operator configuring the same fact on every
    machine: a client already knows the admin's url, so the list is one hop
    away and it picks from that rather than restating it locally.
    """
    from aiforge_core.memory.sync import group, identity

    rows = group.known()
    # ``default`` is what a client joins when nobody picks — the first group the
    # operator published. Named in the response so the client does not have to
    # know that "first" is the rule.
    return {"groups": rows, "default": group.default_of(rows),
            "admin": identity.self_id()}


@router.get("/api/memory/sync/manifest", responses={
    400: {"description": "Bad group name"},
    404: {"description": "No such group"}})
def sync_manifest(group: GroupQ = "") -> dict:
    """What a spoke may pull, plus who we are.

    ``admin`` is how a spoke learns whose fold to trust without the operator
    configuring the same id on every machine (``sync.role.remember_admin_id``).
    """
    from aiforge_core.memory.sync import identity, inbox, role

    with _scope(group):
        return {"manifest": inbox.downstream(), "admin": identity.self_id(),
                "role": role.role(), "group": group}


@router.get("/api/memory/sync/blob/{digest}", responses={
    400: {"description": "Bad group name"},
    404: {"description": "Not found"}})
def sync_blob(digest: str, group: GroupQ = "") -> Response:
    from aiforge_core.memory.sync import inbox
    from aiforge_core.memory.sync import manifest as _man

    digest = (digest or "").strip().lower()
    with _scope(group):
        if digest not in {str(e.get("hash") or "") for e in inbox.downstream()}:
            # Not "no such file": a hash we hold but do not advertise is one a
            # spoke has no business reading back — its own raw notes, another
            # spoke's, or (now) another GROUP's, which is why the membership
            # test is made inside the scope rather than against the whole tree.
            raise HTTPException(404, f"no blob: {digest}")
        path = _man.path_for_hash(digest)
        if path is None:
            raise HTTPException(404, f"no blob: {digest}")
        return Response(content=path.read_bytes(), media_type="text/markdown")


@router.post("/api/memory/sync/offer", responses={
    400: {"description": "Bad request"},
    404: {"description": "No such group"},
    413: {"description": "Payload too large"}})
async def sync_offer(request: Request) -> dict:
    """A spoke offers its manifest; we answer with what we do not hold."""
    payload = await _read_json(request)
    from aiforge_core.memory.sync import inbox

    entries = payload.get("entries")
    with _scope(str(payload.get("group") or "")):
        inbox.seen(str(payload.get("peer") or ""))
        return {"want": inbox.wanted(entries if isinstance(entries, list) else [])}


@router.post("/api/memory/sync/push", responses={
    400: {"description": "Bad request"},
    404: {"description": "No such group"},
    413: {"description": "Payload too large"}})
async def sync_push(request: Request) -> dict:
    """A spoke sends one record we asked for. ``applied`` says whether it stuck."""
    payload = await _read_json(request)
    from aiforge_core.memory.sync import inbox

    try:
        body = base64.b64decode(str(payload.get("body") or ""), validate=True)
    except Exception:  # noqa: BLE001 — undecodable bytes are a refused record
        raise HTTPException(400, "body is not valid base64") from None
    entry = payload.get("entry")
    with _scope(str(payload.get("group") or "")):
        return {"applied": inbox.accept(str(payload.get("peer") or ""),
                                        entry if isinstance(entry, dict) else {}, body)}


@router.get("/api/memory/sync/status")
def sync_status() -> dict:
    """Everything the settings screen needs, in one call.

    Served from the record the cycle writes rather than probed live. A UI must
    not be the thing that discovers the admin is down: a probe on a page load is
    a twenty-second hang the moment it matters, and the answer it would give is
    already on disk.
    """
    from aiforge_core.memory.sync import group, redact, role, status

    row = dict(status.read())
    row.setdefault("state", "no-admin" if not role.admin_url() else "unknown")
    row.setdefault("admin", role.admin_url())
    row.setdefault("group", group.selected())
    row.setdefault("groups_available", [])
    row.setdefault("reachable", False)
    row.setdefault("pending", 0)
    row.setdefault("pushed_total", 0)
    row.setdefault("blocked", {})
    row.setdefault("last_ok", None)
    row.setdefault("last_error", None)
    row["role"] = role.role()
    # The screen has to distinguish "you may edit this" from "an operator pinned
    # it in .env, and your edit would be ignored".
    row["admin_pinned"] = bool(_env_admin())
    row["group_pinned"] = bool(_env_group())
    row["rules"] = redact.explain()
    row["recent_blocks"] = status.blocks()[:20]
    return row


@router.put("/api/memory/sync/group", responses={400: {"description": "Bad name"}})
async def choose_group(request: Request) -> dict:
    """This client joins a group. Persisted, so the choice survives a restart."""
    payload = await _read_json(request)
    from aiforge_core.memory.sync import group

    try:
        return {"group": group.choose(str(payload.get("group") or ""))}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/api/memory/sync/now")
def sync_now() -> dict:
    """Run one cycle on demand. Returns the rows the cycle produced.

    ``run_once`` never raises and bounds itself with the same cycle budget the
    daemon uses, so this cannot become a way to hang the API.
    """
    from aiforge_core.memory.sync import loop

    return {"rows": loop.run_once()}


@router.put("/api/memory/sync/admin", responses={400: {"description": "Refused"}})
async def set_admin(request: Request) -> dict:
    """Point this machine at an admin, from the settings screen.

    ``AIFORGE_ADMIN_URL`` still wins when it is set, so an operator who pinned
    it in ``.env`` is not overridden by a click; the saved value is for the
    machine where nobody edits files. An empty url clears the saved value.

    Runs one cycle straight away, so the screen can show the group list the new
    admin publishes instead of asking the operator to wait for the next tick.
    """
    payload = await _read_json(request)
    from aiforge_core.memory.sync import loop, role

    try:
        url = role.set_admin_url(str(payload.get("url") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"admin": url, "pinned_by_env": bool(_env_admin()), "rows": loop.run_once()}


def _env_admin() -> str:
    import os

    return (os.environ.get("AIFORGE_ADMIN_URL") or "").strip()


def _env_group() -> str:
    import os

    return (os.environ.get("AIFORGE_SYNC_GROUP") or "").strip()
