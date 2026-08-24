"""Memory routes (/api/memory/*) — split out of api.py (APIRouter).

Stats/search + markdown-file notes + OKR graph + ingestion sources +
destructive admin (clear). Every handler keeps its inline function-local
imports and behaviour exactly as it was in api.py; the module-level helpers,
request models, and the reindex-all singleton moved here VERBATIM alongside
the endpoints that use them.
"""
from __future__ import annotations

import logging
import asyncio
import os

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from aiforge_core.api._shared import _db
from aiforge_core.runtime.background import spawn as _spawn

router = APIRouter()

_af_log = logging.getLogger("aiforge")


_reindex_all_lock = __import__("threading").Lock()


_reindex_all_at = [0.0]     # monotonic ts of the last spawn (debounce)


def _spawn_reindex_all() -> None:
    """Re-index every repo/docs source in a SEPARATE PROCESS (CPU-heavy →
    keeps it off the API's GIL, like _spawn_index). Debounced so the daily fire
    and a manual trigger landing together don't launch two full sweeps at once
    (per-source leases already prevent double-indexing a single source; this
    just avoids the wasted second pass)."""
    import sys
    import time as _t
    with _reindex_all_lock:
        if _t.monotonic() - _reindex_all_at[0] < 120:
            _af_log.info("reindex-all skipped — one ran <120s ago (debounce)")
            return
        _reindex_all_at[0] = _t.monotonic()
    from aiforge_core.runtime import background as _bg
    _bg.spawn(name="reindex-all", kind="process",
              argv=[sys.executable, "-m",
                    "aiforge_core.runtime.memory_ingest", "--all"])


def _neo4j_stats() -> dict:
    """Node counts per label from the graph (one row per label, plus a
    grand total). Soft — returns zeros on any driver error."""
    try:
        from neo4j import GraphDatabase

        from aiforge_core.memory.neo4j_conn import neo4j_params
        uri, user, pw = neo4j_params()
        drv = GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001
        # Don't echo the raw driver error — it can embed the bolt URI / creds.
        return {"backend": "neo4j", "total": 0, "wings": [],
                "error": type(exc).__name__}
    try:
        with drv.session() as s:
            total = s.run("MATCH (n) RETURN count(n) AS n").single()["n"]
            rows = s.run(
                "MATCH (n) UNWIND labels(n) AS label "
                "RETURN label, count(*) AS n ORDER BY n DESC LIMIT 30"
            )
            wings = [
                {"tier": "graph", "wing": r["label"], "n": r["n"], "embedded": r["n"]}
                for r in rows
            ]
        return {"backend": "neo4j", "total": int(total), "wings": wings}
    except Exception as exc:  # noqa: BLE001
        # Don't echo the raw driver error — it can embed the bolt URI / creds.
        return {"backend": "neo4j", "total": 0, "wings": [],
                "error": type(exc).__name__}
    finally:
        try:
            drv.close()
        except Exception:
            pass


@router.get("/api/memory/stats")
def memory_stats() -> dict:
    from aiforge_core.memory import backend_select as _bsel
    backend = _bsel.memory_backend()
    if backend == "neo4j":
        return _neo4j_stats()
    if backend == "sqlite":
        from aiforge_core.memory import sqlite_memory as _sqlmem
        s = _sqlmem.stats()
        wings = [{"tier": "embedded", "wing": k, "n": v, "embedded": v}
                 for k, v in s.get("by_kind", {}).items()]
        return {"backend": "sqlite", "total": s.get("total", 0), "wings": wings,
                # the OKR node-DAG view is consolidated out by default; the UI
                # hides its panel unless the DAG is explicitly enabled.
                "okr_dag": os.environ.get("AIFORGE_OKR_DAG", "0") == "1"}
    with _db() as c, c.cursor() as cur:
        cur.execute(
            "SELECT tier, wing, COUNT(*) AS n, "
            "COUNT(embedding) AS embedded "
            "FROM memories GROUP BY tier, wing "
            "ORDER BY tier, wing"
        )
        rows = cur.fetchall()
    return {"backend": "postgres", "wings": rows}


def _search_origin(h: dict) -> str:
    """Bucket a unified-query hit for the UI/API split.

    ``vector`` — retrieved by the semantic embedding index (sqlite-vec KNN /
    Neo4j vector). ``md`` — backed by / retrieved from a markdown memory file
    (keyword-BM25 over md text, brief rows, link expansion). ``other`` —
    everything else (afm bundle, ticket, graphify, cross-repo…)."""
    ch = str(h.get("channel") or "").strip()
    if ch in ("memory", "vector"):
        return "vector"
    if ch in ("keyword", "linked"):
        return "md"
    src = str(h.get("source") or "")
    if src.startswith("compacted:") or src.startswith("md:"):
        return "md"
    return "other"


@router.get("/api/memory/search")
def memory_search(q: str = Query(..., min_length=2),
                  role: str = Query("sr_developer"),
                  top_k: int = Query(12, le=50)) -> dict:
    """Hybrid memory search, results SEPARATED by origin.

    Returns ``{"query", "used_sources", "groups": {"vector": [...], "md":
    [...], "other": [...]}, "hits": [...]}`` — ``groups`` splits semantic
    vector-index hits from md-file/keyword hits (and everything else); ``hits``
    is the same rows flat (rank order) for any caller that wants them merged.
    Each row carries an ``origin`` field mirroring its group."""
    from aiforge_core.memory import backend_select as _bsel
    backend = _bsel.memory_backend()

    def _shape(h: dict, tier: str) -> dict:
        origin = _search_origin(h)
        return {
            "tier": tier, "origin": origin, "channel": h.get("channel"),
            "wing": h.get("kind") or h.get("source"),
            "source": h.get("source"),
            "linked": bool(h.get("linked")),
            "text": (h.get("text") or "")[:800], "score": h.get("score"),
            "metadata": {"ticket": h.get("ticket"), "repo": h.get("repo")},
        }

    def _grouped(flat: list[dict], group_rows: list[dict], res: dict) -> dict:
        # Groups are built from the PRE-dedup ranked list so a brief that matched
        # BOTH the vector KNN and the keyword index shows in BOTH the vector and
        # md buckets (overlap is expected), instead of the vector index hiding
        # behind whichever copy won cross-channel dedup. Dedup WITHIN each bucket
        # (by text) and cap at top_k. The flat `hits` stay cross-channel-deduped
        # (what agents consume).
        groups: dict[str, list[dict]] = {"vector": [], "md": [], "other": []}
        seen: dict[str, set] = {"vector": set(), "md": set(), "other": set()}
        for r in group_rows:
            g = r["origin"]
            key = (r.get("text") or "")[:200]
            if key in seen[g]:
                continue
            seen[g].add(key)
            if len(groups[g]) < top_k:
                groups[g].append(r)
        return {"query": q, "used_sources": res.get("used_sources", []),
                "groups": groups, "hits": flat}

    if backend in ("sqlite", "neo4j"):
        # Full HYBRID (same as the agents' memory_lookup): semantic KNN
        # (sqlite-vec) + keyword/BM25 + spell-correction + link expansion.
        from aiforge_core.memory import unified_query as _uq
        res = _uq.query(q, role=role, limit=top_k)
        tier = "embedded" if backend == "sqlite" else "graph"
        flat = [_shape(h, tier) for h in res.get("hits", [])]
        grp = [_shape(h, tier) for h in res.get("ranked", res.get("hits", []))]
        return _grouped(flat, grp, res)

    from aiforge_core.memory.store import Memory
    m = Memory()
    hits = m.search(q, role=role, top_k=top_k)
    rows = [
        {"tier": h.tier, "origin": "other", "channel": None,
         "wing": h.wing, "source": h.source, "linked": False,
         "text": h.text[:800], "score": h.score, "metadata": h.metadata}
        for h in hits
    ]
    return _grouped(rows, rows, {"used_sources": []})


class _MemSourceBody(BaseModel):
    kind: str = Field(..., description="repo | docs | url | file")
    location: str = Field(..., min_length=1, description="path or URL")
    name: str | None = Field(None)


class _ValidatePathBody(BaseModel):
    location: str = Field(..., min_length=1, description="path to validate")


@router.post("/api/memory/validate-path")
def memory_validate_path(body: _ValidatePathBody) -> dict:
    """Pre-flight a repo/dir path BEFORE indexing — returns the resolved abs
    path + code/doc file counts so a wrong/empty/relative path is caught up
    front (the #1 cause of an index that silently produces 0 units)."""
    from aiforge_core.runtime.memory_ingest import validate_path
    return validate_path(body.location)


@router.get("/api/memory/files")
def memory_files_list() -> list[dict]:
    from aiforge_core.memory import md_store
    return md_store.list_files()


@router.get("/api/memory/files/{name}")
def memory_files_get(name: str) -> dict:
    from aiforge_core.memory import md_store
    d = md_store.read_file(name)
    if d is None:
        raise HTTPException(404, f"no memory file: {name}")
    return d


class _MemFileBody(BaseModel):
    title: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    kind: str = Field("note")
    tags: list[str] | None = Field(None)


@router.post("/api/memory/files", status_code=201)
def memory_files_create(body: _MemFileBody) -> dict:
    from aiforge_core.memory import md_store
    return md_store.write(body.title, body.text, kind=body.kind,
                          tags=body.tags or [], source="manual")


@router.post("/api/memory/files/ingest")
def memory_files_ingest() -> dict:
    """(Re)ingest every md file in the memory dir into the search backend."""
    from aiforge_core.memory import md_store
    return md_store.ingest_dir()


@router.post("/api/memory/files/compact")
def memory_files_compact(group_by: str = Query("topic"),
                         min_group: int = Query(1, ge=1),
                         dry_run: bool = Query(False),
                         summarize: bool = Query(True),
                         force: bool = Query(False),
                         model_role: str = Query("learner")) -> dict:
    """Consolidate per-session md memories into fewer standardized files.

    Group by ``topic`` (default — an LLM clusters notes into coherent topical
    files, so you get several browsable memories, not one blob per kind), or
    ``kind`` / ``tag`` / ``source``. With ``summarize``
    (default) an available LLM (``model_role``'s primary→cloud chain) rewrites
    each group into a compact, deduped document so the file stays small; falls
    back to a plain merge when no model is reachable. ``dry_run=true`` returns
    the plan without writing. Originals are archived (reversible)."""
    from aiforge_core.memory import md_store
    res = md_store.compact(group_by=group_by, min_group=min_group,
                           model_role=model_role,
                           dry_run=dry_run, summarize=summarize, force=force)
    # Apply the cross-brief rules (merge/dedupe/contradict/sweep/lint/link) so the
    # 'Compact' button rewrites with ALL rules, not just the fold. Skip on a
    # dry-run plan (mutating) and when the caller runs the full 'Compact all'
    # (force) path, which already runs these as its own steps.
    if not dry_run and not force:
        try:
            res = dict(res) if isinstance(res, dict) else {"compact": res}
            res["rules"] = md_store.finalize_briefs(role=model_role)
        except Exception as exc:  # noqa: BLE001
            res["rules"] = {"error": str(exc)}
    return res


_compact_all_state: dict = {"running": False, "started_at": None,
                            "steps": [], "current": None, "sub": None,
                            "done": False, "result": None, "error": None}


@router.post("/api/memory/dedupe")
def memory_dedupe() -> dict:
    """Remove duplicate OKR nodes + duplicate chat sessions (from repeated /
    non-idempotent migrations). Also runs inside 'Compact all'."""
    from aiforge_core.memory import migrations
    return migrations.dedupe_all()


@router.post("/api/memory/compact-all")
def memory_compact_all() -> dict:
    """COMPACT ALL — redo everything from scratch: tidy legacy briefs, re-run the
    LLM (chonkie) over EVERY brief, rebuild OKR repo cards, re-ingest the search
    index. Heavy (full LLM pass) → runs in the BACKGROUND; poll
    /api/memory/compact-all/status for progress. The plain 'Compact' only folds
    NEW files (fast, synchronous)."""
    import time as _t
    if _compact_all_state["running"]:
        return {"ok": True, "already_running": True,
                "current": _compact_all_state["current"]}
    from aiforge_core.memory import migrations
    _compact_all_state.update(running=True, started_at=_t.time(), steps=[],
                              current=None, sub=None, done=False,
                              result=None, error=None)

    def _on_step(name, phase, result):
        if phase == "run":
            _compact_all_state["current"] = name
            _compact_all_state["sub"] = None
        elif phase == "progress":
            r = result or {}
            _compact_all_state["current"] = name
            _compact_all_state["sub"] = {"done": r.get("done"),
                                         "total": r.get("total"),
                                         "key": r.get("key")}
        else:
            _compact_all_state["steps"].append({"name": name, "result": result})
            _compact_all_state["current"] = name
            _compact_all_state["sub"] = None

    def _run():
        try:
            r = migrations.force_recompact_all(on_step=_on_step)
            _compact_all_state["result"] = r
        except Exception as exc:  # noqa: BLE001
            _compact_all_state["error"] = str(exc)
        finally:
            _compact_all_state.update(running=False, done=True, current=None)

    _spawn(_run, name="compact-all")
    return {"ok": True, "started": True}


@router.get("/api/memory/compact-all/status")
def memory_compact_all_status() -> dict:
    """Progress of a running 'Compact all' — {running, current step, completed
    steps, done, result}. UI polls this to show a spinner + progress."""
    import time as _t
    s = _compact_all_state
    return {
        "running": s["running"], "done": s["done"], "current": s["current"],
        "sub": s["sub"],                       # {done,total,key} per-brief progress
        "steps_done": [x["name"] for x in s["steps"]],
        "total_steps": 15, "error": s["error"],  # matches force_recompact_all steps
        "elapsed_s": round(_t.time() - s["started_at"], 1) if s["started_at"] else 0,
        "result": s["result"] if s["done"] else None,
    }


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


@router.post("/api/memory/files/cleanup")
def memory_files_cleanup(dry_run: bool = Query(False),
                         model_role: str = Query("learner")) -> dict:
    """One-time tidy: fold id-keyed (compacted-<id>) and per-kind compacted
    briefs back into meaningful, tagged TOPIC briefs; archive the originals
    (reversible). ``dry_run=true`` lists what would be folded without touching
    disk."""
    from aiforge_core.memory import md_store
    return md_store.cleanup_legacy_compacted(dry_run=dry_run, model_role=model_role)


@router.delete("/api/memory/files/{name}")
def memory_files_delete(name: str) -> dict:
    from aiforge_core.memory import md_store
    return {"deleted": md_store.delete_file(name), "name": name}


def _spawn_index(source_id: int) -> None:
    """Kick off ``run_index`` in a SEPARATE PROCESS, not a thread.

    Indexing is CPU-bound (tree-sitter parsing + chunking) and holds the GIL
    for long stretches; in an api thread it starves uvicorn's asyncio event
    loop and wedges every request — health, the UI, and the public tunnel all
    hang for the whole (minutes-long, CPU-embedding) index. A subprocess has
    its own GIL, so the api stays responsive. Detached + non-blocking; the
    child updates the source row's status itself."""
    import sys

    from aiforge_core.runtime import background as _bg
    h = _bg.spawn(name=f"index-{source_id}", kind="process",
                  argv=[sys.executable, "-m",
                        "aiforge_core.runtime.memory_ingest", str(source_id)])
    if h is None:   # process launch failed → fall back to a thread
        from aiforge_core.runtime.memory_ingest import run_index
        _bg.spawn(lambda: run_index(source_id), name=f"index-{source_id}-thread")


@router.get("/api/memory/sources")
def memory_sources_list() -> list[dict]:
    from aiforge_core.runtime import memory_sources as _ms
    return _ms.list_sources()


@router.post("/api/memory/sources", status_code=201)
def memory_sources_create(body: _MemSourceBody) -> dict:
    """Register a memory source. ``repo``/``docs`` sources auto-start a full
    multi-layer background index immediately (chunks + tree-sitter symbols +
    graphify); ``url``/``file`` stay manual (cheap, index via /index)."""
    from aiforge_core.runtime import memory_sources as _ms
    try:
        src = _ms.create(body.kind, body.location, body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if body.kind in ("repo", "docs"):
        _ms.set_status(src["id"], "indexing", error=None)
        _spawn_index(src["id"])
        src = {**src, "status": "indexing"}
    return src


@router.post("/api/memory/sources/upload", status_code=201)
async def memory_sources_upload(file: UploadFile = File(...),
                                name: str | None = Form(None)) -> dict:
    """Upload a single file to ingest. Saved under the config dir, then
    registered as a ``file`` source (index it with the /index endpoint)."""
    from aiforge_core.runtime import memory_sources as _ms
    dest_dir = os.path.join(
        os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")),
        "memory-files")
    os.makedirs(dest_dir, exist_ok=True)
    safe = os.path.basename(file.filename or "upload.txt")
    dest = os.path.join(dest_dir, safe)
    payload = await file.read()

    def _write() -> None:
        with open(dest, "wb") as fh:
            fh.write(payload)
    # Off the event loop: an upload can be large, and a blocking write here
    # stalls every other request the server is handling.
    await asyncio.to_thread(_write)
    return _ms.create("file", dest, name or safe)


@router.delete("/api/memory/sources/{source_id}", status_code=204)
def memory_sources_delete(source_id: int) -> None:
    from aiforge_core.runtime import memory_sources as _ms
    if not _ms.delete(source_id):
        raise HTTPException(404, f"source {source_id} not found")


@router.post("/api/memory/sources/{source_id}/index")
def memory_sources_index(source_id: int) -> dict:
    """Kick off background indexing of a source into memory."""
    from aiforge_core.runtime import memory_sources as _ms
    src = _ms.get(source_id)
    if not src:
        raise HTTPException(404, f"source {source_id} not found")
    _ms.set_status(source_id, "indexing", error=None)
    _spawn_index(source_id)
    return {**src, "status": "indexing"}


@router.post("/api/memory/reindex-all")
def memory_reindex_all() -> dict:
    """Re-index EVERY registered repo/docs source now (same sweep the daily
    job runs). Fires in a background process; returns immediately."""
    from aiforge_core.runtime import memory_sources as _ms
    n = sum(1 for s in _ms.list_sources() if s.get("kind") in ("repo", "docs"))
    _spawn_reindex_all()
    return {"ok": True, "reindexing": n}


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


@router.post("/api/memory/clear/{store}")
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


@router.post("/api/memory/clear-all")
def memory_clear_all_ep(body: "_MemConfirmBody | None" = None) -> dict:
    """Wipe DATA across every memory store, preserving source registrations +
    config (their index state is reset to idle so they can be re-indexed).
    Requires ``{confirm:true}``."""
    from aiforge_core.memory import admin as _admin
    if not (body and body.confirm):
        raise HTTPException(400, "confirm=true required to wipe all memory")
    return _admin.clear_all()
