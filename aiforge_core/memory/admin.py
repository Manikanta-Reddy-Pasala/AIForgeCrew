"""Memory admin — cross-store overview + per-datasource destructive clear.

Gives the operator a high-level, blank-slate-killing view of EVERY memory
store the app writes to, plus an idempotent "empty this store" action per
datasource. Two hard rules:

  * **Data only, never config.** A clear wipes indexed DATA (graph nodes,
    embeddings, SQLite units, on-disk notes, chat history). It NEVER deletes
    the user's registered memory *sources* (repos/dirs) or any settings —
    those survive so the user can simply re-index to repopulate. After a
    full wipe each source's index state is reset to idle/units=0.

  * **Scoped graph deletes.** The Neo4j graph is potentially shared, so we
    NEVER emit a blanket ``MATCH (n) DETACH DELETE n``. Each graph datasource
    deletes ONLY its own AIForge-owned labels / source tag:

        graph_facts → :Memory / :Observation_v2 / :Decision_v2 / :Fact / :MemoryBlock
        symbols     → :Symbol / :Class / :Method / :Endpoint / :File
                      (DETACH DELETE also drops CALLS/EXTENDS/IMPLEMENTS/IMPORTS/DEFINES)
        chunks      → :Chunk_v2 / :Chunk
        graphify    → :GraphifyNode + any node tagged source='graphify'

Every store soft-fails independently: one unreachable store (e.g. the graph
when running on the SQLite backend) never breaks the whole overview / clear.
"""
from __future__ import annotations

import os

from aiforge_core.memory import backend_select

# Preserved after any clear — surfaced in every clear result so the UI can
# reassure the operator their registered repos/settings are intact.
_PRESERVE_NOTE = "config + source registrations preserved — re-index to repopulate"

# Per-graph-datasource AIForge-owned label allowlists. DETACH DELETE on the
# node also removes its relationships, so the symbol edges (CALLS/EXTENDS/…)
# go with the :Symbol nodes.
_GRAPH_LABELS: dict[str, list[str]] = {
    "graph_facts": ["Memory", "Observation_v2", "Decision_v2", "Fact",
                    "MemoryBlock"],
    "symbols": ["Symbol", "Class", "Method", "Endpoint", "File"],
    "chunks": ["Chunk_v2", "Chunk"],
}

# Observation_v2 kinds that are ingested repo CONTENT (shown as chunks, not
# facts). memory_ingest writes source files as kind=code / docs as kind=doc.
_CHUNK_KINDS = {"code", "doc"}

_SYMBOL_RELS = ["CALLS", "EXTENDS", "IMPLEMENTS", "IMPORTS", "DEFINES"]
_GRAPHIFY_LABEL = "GraphifyNode"
_GRAPHIFY_SOURCE = "graphify"

# Cypher that matches graphify-owned nodes: the catch-all :GraphifyNode label
# OR any node explicitly tagged with source='graphify' (single value or in a
# ``sources`` list, per graphify_loader's coexistence tagging).
_GRAPHIFY_MATCH = (
    "MATCH (n) WHERE n:GraphifyNode OR n.source = $src "
    "OR $src IN coalesce(n.sources, [])"
)

# Datasources a clear can target. NOTE: 'sources' is deliberately absent —
# registrations are config, not data, and must survive a wipe.
CLEARABLE = ["graph_facts", "symbols", "graphify", "chunks",
             "sqlite", "md_files", "chat"]

_GRAPH_STORES = ("graph_facts", "symbols", "graphify", "chunks")


# ───────────────────────────── Neo4j plumbing ───────────────────────────────

def _neo4j_configured() -> bool:
    """True only when a graph backend is actually in play — so a fresh SQLite
    clone never probes a random bolt port. Mirrors backend_select's env gate."""
    return bool(
        os.environ.get("AIFORGE_NEO4J_URI")
        or os.environ.get("NEO4J_URI")
        or backend_select.memory_backend() == "neo4j"
    )


def _graph_driver():
    """Build a Neo4j driver from the shared connection params. Isolated in one
    function so tests can monkeypatch it with a fake driver."""
    from neo4j import GraphDatabase

    from aiforge_core.memory.neo4j_conn import neo4j_params
    uri, user, pw = neo4j_params()
    # Suppress "label/property does not exist" notifications: the overview
    # queries labels (Symbol/Chunk_v2/GraphifyNode) + props (source/sources)
    # that legitimately don't exist until something is indexed — Neo4j 5+ warns
    # loudly on every call, flooding the logs. OFF is correct for count/probe
    # queries where a missing label is an expected 0, not a mistake.
    try:
        return GraphDatabase.driver(uri, auth=(user, pw), connection_timeout=4,
                                    notifications_min_severity="OFF")
    except TypeError:  # older driver without the kwarg
        return GraphDatabase.driver(uri, auth=(user, pw), connection_timeout=4)


def _graph_snapshot() -> dict:
    """One round-trip: per-label node counts, per-type relationship counts, and
    the distinct graphify-owned node count. Raises on driver/connection error
    (callers soft-fail per store)."""
    drv = _graph_driver()
    try:
        with drv.session() as s:
            labels = {
                r["label"]: int(r["n"])
                for r in s.run("MATCH (n) UNWIND labels(n) AS label "
                               "RETURN label, count(*) AS n")
            }
            rels = {
                r["t"]: int(r["n"])
                for r in s.run("MATCH ()-[r]->() "
                               "RETURN type(r) AS t, count(*) AS n")
            }
            gf = s.run(f"{_GRAPHIFY_MATCH} RETURN count(DISTINCT n) AS n",
                       src=_GRAPHIFY_SOURCE).single()["n"]
            # Ingested repo CONTENT is written as Observation_v2 with
            # kind=code|doc (not a Chunk_v2 label), so a label-only count files
            # it under "facts" and leaves the "Code/doc chunks" tile at 0. Split
            # Observation_v2 by kind so content shows as chunks, facts as facts.
            obs_kinds = {
                (r["kind"] or "?"): int(r["n"])
                for r in s.run("MATCH (n:Observation_v2) "
                               "RETURN n.kind AS kind, count(*) AS n")
            }
        return {"labels": labels, "rels": rels, "graphify": int(gf or 0),
                "obs_kinds": obs_kinds}
    finally:
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass


# ─────────────────────────────── overview ───────────────────────────────────

def _graph_sections() -> dict:
    """Build the four graph datasource sections. If the graph isn't configured
    or is unreachable, each section soft-fails to available:False (never raises)."""
    if not _neo4j_configured():
        reason = "graph backend not configured (running on SQLite)"
        return {k: {"available": False, "reason": reason} for k in _GRAPH_STORES}
    try:
        snap = _graph_snapshot()
    except Exception as exc:  # noqa: BLE001
        reason = type(exc).__name__
        return {k: {"available": False, "reason": reason} for k in _GRAPH_STORES}

    labels = snap["labels"]
    obs_kinds = snap.get("obs_kinds", {})
    # Content chunks live as Observation_v2 kind∈{code,doc}; everything else
    # under Observation_v2 is a real fact/decision/learning.
    content_obs = sum(n for k, n in obs_kinds.items() if k in _CHUNK_KINDS)
    out: dict = {}
    for store in ("graph_facts", "symbols", "chunks"):
        counts = {lbl: labels.get(lbl, 0) for lbl in _GRAPH_LABELS[store]
                  if labels.get(lbl, 0)}
        section = {"available": True, "labels": counts,
                   "total": sum(counts.values())}
        if store == "graph_facts":
            # Don't count ingested code/doc content as "facts".
            if "Observation_v2" in counts and content_obs:
                counts["Observation_v2"] = max(
                    0, counts["Observation_v2"] - content_obs)
                if not counts["Observation_v2"]:
                    counts.pop("Observation_v2")
            section["total"] = max(0, section["total"] - content_obs)
        if store == "chunks":
            # Fold ingested code/doc Observation_v2 in as the content chunks.
            if content_obs:
                for k in _CHUNK_KINDS:
                    if obs_kinds.get(k):
                        counts[f"code/doc:{k}"] = obs_kinds[k]
                section["total"] = section["total"] + content_obs
        if store == "symbols":
            section["relationships"] = {
                t: snap["rels"].get(t, 0) for t in _SYMBOL_RELS
                if snap["rels"].get(t, 0)
            }
        out[store] = section
    out["graphify"] = {"available": True, "count": snap["graphify"]}
    return out


def _sqlite_section() -> dict:
    from aiforge_core.memory import sqlite_memory
    s = sqlite_memory.stats()
    return {"total": s.get("total", 0), "by_kind": s.get("by_kind", {}),
            "path": s.get("db_path")}


def _md_section() -> dict:
    from aiforge_core.memory import md_store
    d = md_store.memory_dir()
    files = list(d.glob("*.md"))
    total = sum(p.stat().st_size for p in files if p.is_file())
    return {"count": len(files), "bytes": int(total), "dir": str(d)}


def _chat_section() -> dict:
    from aiforge_core.runtime import chat_store
    sessions = chat_store.list_sessions()
    messages = sum(int(s.get("message_count") or 0) for s in sessions)
    return {"sessions": len(sessions), "messages": messages}


def _sources_section() -> dict:
    """VIEW ONLY — the registered repos/dirs + their status. Never cleared."""
    from aiforge_core.runtime import memory_sources
    items = memory_sources.list_sources()
    return {"count": len(items),
            "by_status": memory_sources.status_counts(),
            "items": items}


def _safe(fn) -> dict:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)}


def memory_overview() -> dict:
    """Per-store high-level breakdown across every memory datasource.

    Shape::

        {"backend": "sqlite"|"neo4j"|"postgres",
         "stores": {
            "graph_facts": {"available": bool, "labels": {..}, "total": n} | {"available": False, ...},
            "symbols":     {"available": bool, "labels": {..}, "total": n, "relationships": {..}} | ...,
            "graphify":    {"available": bool, "count": n} | ...,
            "chunks":      {"available": bool, "labels": {..}, "total": n} | ...,
            "sqlite":      {"total": n, "by_kind": {..}, "path": ".."},
            "md_files":    {"count": n, "bytes": n, "dir": ".."},
            "chat":        {"sessions": n, "messages": n},
            "sources":     {"count": n, "by_status": {..}, "items": [..]},  # VIEW ONLY
         }}

    Every store soft-fails independently — one down store never breaks the rest.
    """
    stores: dict = {}
    stores.update(_safe(_graph_sections))
    # _graph_sections returns the 4 graph stores directly; on a hard failure of
    # the whole helper _safe wraps it as one dict — normalize that edge case.
    if "graph_facts" not in stores:
        reason = stores.get("reason", "graph error")
        for k in _GRAPH_STORES:
            stores[k] = {"available": False, "reason": reason}
    stores["sqlite"] = _safe(_sqlite_section)
    stores["md_files"] = _safe(_md_section)
    stores["chat"] = _safe(_chat_section)
    stores["sources"] = _safe(_sources_section)
    browser = os.environ.get("AIFORGE_NEO4J_BROWSER_URL") or None
    host = _neo4j_browser_host(browser)
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    bolt = f"neo4j://{user}@{host}:7687" if host else None
    # Zero-friction opt-in: embed the password so Neo4j Browser AUTO-CONNECTS
    # (no prompt). Default OFF — this exposes the neo4j password in the API
    # response, which is only acceptable on a trusted LAN with the weak default
    # password. The NUC launcher sets AIFORGE_NEO4J_BROWSER_EMBED_CREDS=1.
    connect = None
    if host and os.environ.get("AIFORGE_NEO4J_BROWSER_EMBED_CREDS", "").strip().lower() \
            in ("1", "true", "yes", "on"):
        pw = (os.environ.get("AIFORGE_NEO4J_PASSWORD")
              or os.environ.get("NEO4J_PASSWORD") or "password")
        connect = f"neo4j://{user}:{pw}@{host}:7687"
    return {"backend": backend_select.memory_backend(), "stores": stores,
            "neo4j_browser": browser,
            # Bolt connect URL, host + user only (NEVER the password) — prefills
            # Neo4j Browser's connect form; user types the password once.
            "neo4j_bolt": bolt,
            # Full connect URL WITH creds (auto-connect) — only when the
            # operator opted in via AIFORGE_NEO4J_BROWSER_EMBED_CREDS.
            "neo4j_connect": connect,
            "neo4j_user": user}


def _neo4j_browser_host(browser_url: str | None) -> str | None:
    """Host for the bolt connect URL — the browser + bolt share a host (only the
    port differs 7474→7687). Derived from the browser URL, else the bolt URI.
    Returns None for loopback (a link there only works ON the server)."""
    from urllib.parse import urlparse
    host = None
    for candidate in (browser_url,
                      os.environ.get("AIFORGE_NEO4J_URI"),
                      os.environ.get("NEO4J_URI")):
        if not candidate:
            continue
        try:
            host = urlparse(candidate).hostname
        except Exception:  # noqa: BLE001
            host = None
        if host:
            break
    if not host or host in ("127.0.0.1", "localhost"):
        return None
    return host


# ─────────────────────── graph sample (visualization) ───────────────────────

def _clip(s: str, n: int = 40) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _chunk_label(text: str) -> str:
    """First meaningful line of an ingested chunk, minus the leading '# path'
    header memory_ingest prepends. Truncated for the node label."""
    lines = (text or "").splitlines()
    start = 1 if lines and lines[0].lstrip().startswith("#") else 0
    for ln in lines[start:]:
        if ln.strip():
            return _clip(ln, 40)
    for ln in lines:  # fall back to the header line if that's all there is
        if ln.strip():
            return _clip(ln, 40)
    return "(empty)"


def _sample_symbols(s, limit: int) -> dict:
    # Symbol nodes are keyed by `fqn` (fully-qualified name), not `id`; the
    # display name is `simple`. Label the node with the simple name, fall back
    # to the last fqn segment.
    rows = list(s.run(
        "MATCH (n:Symbol) WITH n LIMIT $limit "
        "RETURN n.fqn AS id, coalesce(n.simple, n.fqn) AS label, n.kind AS kind",
        limit=limit))
    nodes = [{"id": r["id"],
              "label": str(r["label"] or (r["id"] or "").split(".")[-1] or ""),
              "kind": r["kind"] or "symbol"}
             for r in rows if r["id"] is not None]
    ids = [n["id"] for n in nodes]
    edges = []
    if ids:
        erows = list(s.run(
            "MATCH (n:Symbol)-[r:CALLS|DEFINES|EXTENDS|IMPLEMENTS]->(m:Symbol) "
            "WHERE n.fqn IN $ids AND m.fqn IN $ids "
            "RETURN n.fqn AS a, m.fqn AS b, type(r) AS t LIMIT 500",
            ids=ids))
        edges = [{"from": r["a"], "to": r["b"], "type": r["t"]} for r in erows]
    return {"available": True, "nodes": nodes, "edges": edges}


def _sample_graphify(s, limit: int) -> dict:
    rows = list(s.run(
        "MATCH (n) WHERE n.source = $src OR n:GraphifyNode WITH n LIMIT $limit "
        "RETURN n.id AS id, coalesce(n.name, n.title, n.id) AS label",
        src=_GRAPHIFY_SOURCE, limit=limit))
    nodes = [{"id": r["id"], "label": str(r["label"] or r["id"] or ""),
              "kind": "graphify"}
             for r in rows if r["id"] is not None]
    ids = [n["id"] for n in nodes]
    edges = []
    if ids:
        erows = list(s.run(
            "MATCH (n)-[r]->(m) WHERE n.id IN $ids AND m.id IN $ids "
            "RETURN n.id AS a, m.id AS b, type(r) AS t LIMIT 500",
            ids=ids))
        edges = [{"from": r["a"], "to": r["b"], "type": r["t"]} for r in erows]
    return {"available": True, "nodes": nodes, "edges": edges}


def _sample_chunks(s, limit: int) -> dict:
    rows = list(s.run(
        "MATCH (n:Observation_v2) WHERE n.kind IN ['code','doc'] "
        "RETURN n.id AS id, n.text AS text, n.kind AS kind LIMIT $limit",
        limit=limit))
    nodes = [{"id": r["id"], "label": _chunk_label(r["text"]),
              "kind": r["kind"] or "chunk"}
             for r in rows if r["id"] is not None]
    return {"available": True, "nodes": nodes, "edges": []}


def _sample_facts(s, limit: int) -> dict:
    rows = list(s.run(
        "MATCH (n) WHERE (n:Observation_v2 AND "
        "NOT coalesce(n.kind, '') IN ['code','doc']) OR n:Decision_v2 "
        "RETURN n.id AS id, coalesce(n.text, n.title, n.id) AS text, "
        "n.kind AS kind LIMIT $limit",
        limit=limit))
    nodes = [{"id": r["id"], "label": _clip(str(r["text"] or r["id"] or ""), 40),
              "kind": r["kind"] or "fact"}
             for r in rows if r["id"] is not None]
    return {"available": True, "nodes": nodes, "edges": []}


def _expand_symbols(s, node_id: str, limit: int) -> dict:
    # UNDIRECTED match so both callers and callees of the node show up; the
    # edge still preserves its true direction via startNode/endNode fqn.
    rows = list(s.run(
        "MATCH (n:Symbol {fqn:$id})-[r:CALLS|DEFINES|EXTENDS|IMPLEMENTS]-(m:Symbol) "
        "RETURN n.fqn AS nid, coalesce(n.simple, n.fqn) AS nlabel, n.kind AS nkind, "
        "m.fqn AS mid, coalesce(m.simple, m.fqn) AS mlabel, m.kind AS mkind, "
        "startNode(r).fqn AS a, endNode(r).fqn AS b, type(r) AS t LIMIT $limit",
        id=node_id, limit=limit))
    nodes: dict = {}
    edges = []
    for r in rows:
        for nid, nlabel, nkind in ((r["nid"], r["nlabel"], r["nkind"]),
                                   (r["mid"], r["mlabel"], r["mkind"])):
            if nid is not None and nid not in nodes:
                nodes[nid] = {"id": nid,
                              "label": str(nlabel or nid.split(".")[-1] or ""),
                              "kind": nkind or "symbol"}
        if r["a"] is not None and r["b"] is not None:
            edges.append({"from": r["a"], "to": r["b"], "type": r["t"]})
    # Ensure the anchor node is present even when it has no matching edges.
    if node_id not in nodes:
        anchor = s.run(
            "MATCH (n:Symbol {fqn:$id}) "
            "RETURN n.fqn AS id, coalesce(n.simple, n.fqn) AS label, n.kind AS kind",
            id=node_id).single()
        if anchor and anchor["id"] is not None:
            nodes[anchor["id"]] = {
                "id": anchor["id"],
                "label": str(anchor["label"] or anchor["id"].split(".")[-1] or ""),
                "kind": anchor["kind"] or "symbol"}
    return {"available": True, "nodes": list(nodes.values()), "edges": edges}


def _expand_graphify(s, node_id: str, limit: int) -> dict:
    rows = list(s.run(
        "MATCH (n {id:$id})-[r]-(m) WHERE (n.source = $src OR n:GraphifyNode) "
        "RETURN n.id AS nid, coalesce(n.name, n.title, n.id) AS nlabel, "
        "m.id AS mid, coalesce(m.name, m.title, m.id) AS mlabel, "
        "startNode(r).id AS a, endNode(r).id AS b, type(r) AS t LIMIT $limit",
        id=node_id, src=_GRAPHIFY_SOURCE, limit=limit))
    nodes: dict = {}
    edges = []
    for r in rows:
        for nid, nlabel in ((r["nid"], r["nlabel"]), (r["mid"], r["mlabel"])):
            if nid is not None and nid not in nodes:
                nodes[nid] = {"id": nid, "label": str(nlabel or nid or ""),
                              "kind": "graphify"}
        if r["a"] is not None and r["b"] is not None:
            edges.append({"from": r["a"], "to": r["b"], "type": r["t"]})
    if node_id not in nodes:
        anchor = s.run(
            "MATCH (n {id:$id}) WHERE (n.source = $src OR n:GraphifyNode) "
            "RETURN n.id AS id, coalesce(n.name, n.title, n.id) AS label",
            id=node_id, src=_GRAPHIFY_SOURCE).single()
        if anchor and anchor["id"] is not None:
            nodes[anchor["id"]] = {"id": anchor["id"],
                                   "label": str(anchor["label"] or anchor["id"] or ""),
                                   "kind": "graphify"}
    return {"available": True, "nodes": list(nodes.values()), "edges": edges}


def _expand_observation(s, node_id: str) -> dict:
    # chunks / graph_facts live as Observation_v2 with no meaningful edges —
    # return just the single node so a click is effectively a no-op.
    anchor = s.run(
        "MATCH (n:Observation_v2 {id:$id}) "
        "RETURN n.id AS id, coalesce(n.text, n.title, n.id) AS text, n.kind AS kind",
        id=node_id).single()
    if not anchor or anchor["id"] is None:
        return {"available": True, "nodes": [], "edges": []}
    kind = anchor["kind"] or ""
    label = (_chunk_label(anchor["text"]) if kind in _CHUNK_KINDS
             else _clip(str(anchor["text"] or anchor["id"] or ""), 40))
    return {"available": True,
            "nodes": [{"id": anchor["id"], "label": label,
                       "kind": kind or "chunk"}],
            "edges": []}


def graph_expand(store: str, node_id: str, limit: int = 40) -> dict:
    """Neighborhood of ONE node in a graph store — the node itself + its
    directly-connected neighbors + the connecting edges. Same shape as
    ``graph_sample`` (``{"available","nodes","edges"}``). Soft-fails to
    available:False on any error (unknown store, missing node, graph not
    configured, driver/query failure) — never raises.
    """
    empty = {"available": False, "nodes": [], "edges": []}
    if store not in _GRAPH_STORES or not _neo4j_configured() or not node_id:
        return empty
    try:
        limit = max(1, min(int(limit), 200))
    except Exception:  # noqa: BLE001
        limit = 40
    try:
        drv = _graph_driver()
    except Exception:  # noqa: BLE001
        return empty
    try:
        with drv.session() as s:
            if store == "symbols":
                return _expand_symbols(s, node_id, limit)
            if store == "graphify":
                return _expand_graphify(s, node_id, limit)
            return _expand_observation(s, node_id)  # chunks / graph_facts
    except Exception:  # noqa: BLE001
        return empty
    finally:
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass


def graph_sample(store: str, limit: int = 60) -> dict:
    """Small node-link sample of ONE graph store, for an in-app SVG preview.

    Returns ``{"available": bool, "nodes": [{"id","label","kind"}],
    "edges": [{"from","to","type"}]}``. Soft-fails to available:False on any
    error (unknown store, graph not configured, driver/query failure) — never
    raises. Edges are returned ONLY between sampled node ids.
    """
    empty = {"available": False, "nodes": [], "edges": []}
    if store not in _GRAPH_STORES or not _neo4j_configured():
        return empty
    try:
        limit = max(1, min(int(limit), 300))
    except Exception:  # noqa: BLE001
        limit = 60
    try:
        drv = _graph_driver()
    except Exception:  # noqa: BLE001
        return empty
    try:
        with drv.session() as s:
            if store == "symbols":
                return _sample_symbols(s, limit)
            if store == "graphify":
                return _sample_graphify(s, limit)
            if store == "chunks":
                return _sample_chunks(s, limit)
            return _sample_facts(s, limit)  # graph_facts
    except Exception:  # noqa: BLE001
        return empty
    finally:
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass


# ──────────────────────────── per-store clear ───────────────────────────────

def _clear_graph_labels(labels: list[str]) -> int:
    """DETACH DELETE every node carrying one of ``labels`` (scoped — never a
    blanket wipe). Relationships on those nodes go with them. Returns the node
    count deleted (from the write summary counters)."""
    drv = _graph_driver()
    try:
        with drv.session() as s:
            res = s.run(
                "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $labels) "
                "DETACH DELETE n",
                labels=labels,
            )
            return int(res.consume().counters.nodes_deleted)
    finally:
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass


def _clear_graphify() -> int:
    drv = _graph_driver()
    try:
        with drv.session() as s:
            res = s.run(f"{_GRAPHIFY_MATCH} DETACH DELETE n",
                        src=_GRAPHIFY_SOURCE)
            return int(res.consume().counters.nodes_deleted)
    finally:
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass


def _clear_md() -> int:
    """Delete root-level per-run captures AND the consolidated briefs in the
    compacted/ subfolder. (Briefs moved out of the root into compacted/, so a
    root-only glob no longer wipes them — that left stale briefs after a clear /
    --clear re-ingest.) The archive/ and memory-archive/ trees are preserved."""
    from aiforge_core.memory import md_store
    n = 0
    for p in list(md_store.memory_dir().glob("*.md")) \
            + list(md_store.briefs_dir().glob("*.md")):
        if p.is_file():
            p.unlink()
            n += 1
    return n


def _clear_target(store: str) -> dict:
    """Return the human-readable description of what a clear deletes, so the
    result (and the confirm dialog) can state it exactly."""
    if store in _GRAPH_LABELS:
        return {"backend": "neo4j",
                "labels": list(_GRAPH_LABELS[store])}
    if store == "graphify":
        return {"backend": "neo4j", "match": ":GraphifyNode + source='graphify'"}
    if store == "sqlite":
        return {"backend": "sqlite", "table": "memory_units"}
    if store == "chat":
        return {"backend": "sqlite/postgres", "table": "chat_sessions+messages"}
    if store == "md_files":
        return {"backend": "disk", "match": "*.md under memory dir"}
    return {}


def clear_store(store: str) -> dict:
    """Delete all DATA in ONE store (idempotent, soft-fails per store).

    ``store`` ∈ ``graph_facts | symbols | graphify | chunks | sqlite |
    md_files | chat``. Unknown store → ValueError (the API maps that to 400).
    Never touches registered sources or configuration.
    """
    if store not in CLEARABLE:
        raise ValueError(f"unknown store: {store}")
    try:
        if store in _GRAPH_LABELS:
            deleted = _clear_graph_labels(_GRAPH_LABELS[store])
        elif store == "graphify":
            deleted = _clear_graphify()
        elif store == "sqlite":
            from aiforge_core.memory import sqlite_memory
            deleted = sqlite_memory.clear()
        elif store == "chat":
            from aiforge_core.runtime import chat_store
            deleted = chat_store.delete_all_sessions()
        elif store == "md_files":
            deleted = _clear_md()
        else:  # pragma: no cover — guarded by CLEARABLE
            raise ValueError(f"unhandled store: {store}")
        return {"store": store, "ok": True, "deleted": int(deleted),
                "target": _clear_target(store), "note": _PRESERVE_NOTE}
    except Exception as exc:  # noqa: BLE001
        return {"store": store, "ok": False, "reason": str(exc),
                "target": _clear_target(store)}


def clear_all() -> dict:
    """Wipe DATA across every store, then reset the still-registered sources'
    index state to idle/units=0 (registrations + config are preserved). Returns
    a per-store result dict; nothing raises."""
    results = {s: clear_store(s) for s in CLEARABLE}
    try:
        from aiforge_core.runtime import memory_sources
        sources_reset = memory_sources.reset_all()
    except Exception as exc:  # noqa: BLE001
        sources_reset = {"ok": False, "reason": str(exc)}
    return {"results": results, "sources_reset": sources_reset,
            "note": _PRESERVE_NOTE}


__all__ = ["memory_overview", "graph_sample", "graph_expand", "clear_store",
           "clear_all", "CLEARABLE"]
