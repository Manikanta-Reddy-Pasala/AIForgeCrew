"""Operator routes for groups and revert (``/api/admin/groups…``).

Loopback-only, on the same ``_require_loopback`` dependency the rest of
``/admin`` uses. These routes CHANGE what the fleet syncs and where it can be
rolled back to, so they belong on the control plane rather than on the open
sync surface — publishing a group is an operator decision, and discovering one
is all a client ever needs.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from aiforge_core.api.routes.admin import _require_loopback

router = APIRouter(dependencies=[Depends(_require_loopback)])


def _grouped(name: str):
    """Scope to ``name``, or to the ungrouped tree when it is empty.

    Refuses an unpublished name rather than creating it: the same rule the sync
    routes enforce, for the same reason — a directory conjured by a typo is a
    pool nobody knows exists.
    """
    from aiforge_core.memory.sync import group

    name = (name or "").strip()
    if name and name not in group.known():
        raise HTTPException(404, f"no such group: {name}")
    return group.scoped(name)


@router.get("/api/admin/groups")
def list_groups() -> dict:
    from aiforge_core.memory.sync import group

    return {"groups": group.known()}


@router.post("/api/admin/groups", responses={400: {"description": "Bad name"}})
async def create_group(request: Request) -> dict:
    from aiforge_core.memory.sync import group

    payload = await request.json()
    name = str((payload or {}).get("name") or "").strip()
    try:
        return {"groups": group.create(name)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


# The group is a QUERY parameter rather than a path segment so that one pair of
# routes covers both shapes: an ungrouped admin has a tree worth reverting too,
# and `/api/admin/groups//snapshots` is not a URL — the empty segment simply
# does not route, which left the commonest deployment with no way in.
@router.get("/api/admin/memory/snapshots",
            responses={404: {"description": "No such group"}})
def memory_snapshots(group: str = "") -> dict:
    from aiforge_core.memory.sync import _io, snapshot

    with _grouped(group):
        return {"group": group, "snapshots": snapshot.listing(_io.root()),
                "keep": snapshot.keep()}


@router.post("/api/admin/memory/revert", responses={
    404: {"description": "No such group or snapshot"}})
async def memory_revert(request: Request, group: str = "") -> dict:
    """Roll a received tree back to a snapshot.

    The state being replaced is itself snapshotted first (``snapshot.revert``),
    so a revert to the wrong stamp is a recoverable mistake rather than a
    destroyed tree.
    """
    from aiforge_core.memory.sync import _io, snapshot

    payload = await request.json()
    to = str((payload or {}).get("to") or "").strip()
    with _grouped(group):
        try:
            replaced = snapshot.revert(_io.root(), to)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
    return {"group": group, "reverted_to": to, "previous_state": replaced}
