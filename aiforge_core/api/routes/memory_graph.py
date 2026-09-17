"""Memory views and resets: the OKR graph and its active project, the overview,
the knowledge graph, and clearing a store or everything."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter()


@router.get("/api/memory/okf")
def memory_okr_graph() -> dict:
    """The OKR-DAG: nodes (by type) + the active KR. Lightweight — frontmatter
    + a body preview, not full bodies."""
    from aiforge_core.memory import okf
    g = okf.build(force=True)
    nodes = []
    for nid, n in g.nodes.items():
        m = n.get("meta") or {}
        nodes.append({"id": nid, "type": n.get("type"),
                      "title": m.get("title") or nid, "status": m.get("status"),
                      "description": m.get("description"),
                      "parent_objective": m.get("parent_objective"),
                      "scope": m.get("scope"), "linked_krs": m.get("linked_krs"),
                      "tags": m.get("tags"), "timestamp": m.get("timestamp"),
                      "preview": (n.get("body") or "")[:200]})
    return {"ok": True, "counts": g.counts(), "active_kr": okf.get_active(),
            "nodes": nodes}


class _OkrActive(BaseModel):
    active_kr: str | None = None


@router.post("/api/memory/okf/active")
def memory_okr_set_active(body: _OkrActive) -> dict:
    from aiforge_core.memory import okf
    return okf.set_active(body.active_kr)


@router.post("/api/memory/okf/migrate")
def memory_okr_migrate() -> dict:
    """Seed the OKR graph from the existing flat topic briefs (each topic → a
    global Learning). Idempotent; briefs left in place."""
    from aiforge_core.memory import okf
    return okf.migrate_from_briefs()


class _MemConfirmBody(BaseModel):
    confirm: bool = Field(False, description="must be true to actually clear")


@router.get("/api/memory/overview")
def memory_overview_ep() -> dict:
    """Per-datasource breakdown: graph (facts/symbols/graphify/chunks), SQLite
    units, on-disk md notes, chat sessions, and registered sources. Each store
    soft-fails independently."""
    from aiforge_core.memory import admin as _admin
    return _admin.memory_overview()


@router.get("/api/memory/graph")
def memory_graph_ep(store: str,
                    limit: int = Query(60, le=300)) -> dict:
    """Small node-link sample of ONE graph store for an in-app SVG preview.
    ``store`` ∈ symbols | graphify | chunks | graph_facts. Soft-fails to
    ``{"available": False, "nodes": [], "edges": []}`` — never raises."""
    from aiforge_core.memory import admin as _admin
    return _admin.graph_sample(store, limit)


@router.get("/api/memory/graph/expand")
def memory_graph_expand_ep(store: str, node_id: str,
                           limit: int = Query(40, le=200)) -> dict:
    """Neighborhood of ONE node — the node + its directly-connected neighbors +
    connecting edges. ``store`` ∈ symbols | graphify | chunks | graph_facts.
    Soft-fails to ``{"available": False, "nodes": [], "edges": []}`` — never
    raises. Powers the in-app interactive graph explorer's click-to-expand."""
    from aiforge_core.memory import admin as _admin
    return _admin.graph_expand(store, node_id, limit)


@router.post("/api/memory/clear/{store}", responses={400: {"description": "Bad request"}})
def memory_clear_store_ep(store: str,
                          body: "_MemConfirmBody | None" = None) -> dict:
    """Clear ALL data in ONE store. ``store`` ∈ graph_facts | symbols |
    graphify | chunks | sqlite | md_files | chat. Requires ``{confirm:true}``.
    Registered sources + configuration are preserved."""
    from aiforge_core.memory import admin as _admin
    if not (body and body.confirm):
        raise HTTPException(400, "confirm=true required to clear a memory store")
    try:
        return _admin.clear_store(store)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/memory/clear-all", responses={400: {"description": "Bad request"}})
def memory_clear_all_ep(body: "_MemConfirmBody | None" = None) -> dict:
    """Wipe DATA across every memory store, preserving source registrations +
    config (their index state is reset to idle so they can be re-indexed).
    Requires ``{confirm:true}``."""
    from aiforge_core.memory import admin as _admin
    if not (body and body.confirm):
        raise HTTPException(400, "confirm=true required to wipe all memory")
    return _admin.clear_all()
