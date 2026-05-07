"""Graphify graph lookup — queryable surface over `graphify-out/graph.json`.

Why this exists
---------------
graphify produces a typed cross-language graph (nodes = files / symbols,
links = ``calls`` / ``uses`` / ``contains`` / ``inherits`` / ``method`` /
``imports_from`` / ``rationale_for``) that AiForgeMemory's vector + AST
ingest doesn't capture — particularly ``rationale_for`` edges, which are
LLM-extracted "this exists because of that" links that aren't derivable
from source alone.

This tool gives agents a read-only handle on that graph: pass a label,
file path, or substring and get back the matched nodes + their k-hop
neighbours typed by relation. KISS: the graph is loaded once per process
and adjacency is computed on first call.

Caller surface
--------------
``graphify_lookup(query, hops=1, max_neighbors=25, repo_root=None)``
returns ``{ok, matches: [...], neighbors: [...]}`` on success. ``ok=False``
when the graph file is missing or unreadable — the agent loop keeps
moving instead of crashing.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

# Module-level cache: repo_root -> (nodes_by_id, adj, file_index, label_index)
_CACHE: dict[str, tuple[dict, dict, dict, dict]] = {}


def _resolve_repo_root(repo_root: str | None) -> Path:
    if repo_root:
        return Path(repo_root).expanduser().resolve()
    env = os.environ.get("AIFORGE_REPO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # Fallback: walk up from this file until graphify-out/ exists.
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        if (p / "graphify-out" / "graph.json").is_file():
            return p
    return here.parents[2]  # AIForgeCrew/ from aiforge_core/runtime/


def _load(repo_root: Path) -> tuple[dict, dict, dict, dict]:
    """Return (nodes_by_id, adj, file_index, label_index). Cached per root."""
    key = str(repo_root)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    gpath = repo_root / "graphify-out" / "graph.json"
    if not gpath.is_file():
        raise FileNotFoundError(f"graphify-out/graph.json not found under {repo_root}")

    g = json.loads(gpath.read_text())
    nodes = g.get("nodes") or []
    links = g.get("links") or g.get("edges") or []

    nodes_by_id: dict[str, dict] = {n["id"]: n for n in nodes if "id" in n}
    adj: dict[str, list[dict]] = defaultdict(list)
    file_index: dict[str, list[str]] = defaultdict(list)
    label_index: dict[str, list[str]] = defaultdict(list)

    for n in nodes:
        nid = n.get("id")
        if not nid:
            continue
        sf = n.get("source_file") or ""
        if sf:
            file_index[sf].append(nid)
        lab = (n.get("label") or "").strip()
        if lab:
            label_index[lab.lower()].append(nid)

    for ln in links:
        s = ln.get("source") or ln.get("_src")
        t = ln.get("target") or ln.get("_tgt")
        if not s or not t:
            continue
        rel = ln.get("relation") or ln.get("type") or "unknown"
        weight = float(ln.get("weight", 1.0) or 1.0)
        conf = ln.get("confidence") or ""
        adj[s].append({"_to": t, "relation": rel, "weight": weight,
                       "confidence": conf, "direction": "out",
                       "source_location": ln.get("source_location", "")})
        adj[t].append({"_to": s, "relation": rel, "weight": weight,
                       "confidence": conf, "direction": "in",
                       "source_location": ln.get("source_location", "")})

    _CACHE[key] = (nodes_by_id, dict(adj), dict(file_index), dict(label_index))
    return _CACHE[key]


def _match_nodes(query: str, nodes_by_id, file_index, label_index,
                 limit: int) -> list[str]:
    """Resolve query → list of node IDs, ordered by match quality."""
    q = query.strip()
    qlow = q.lower()
    seen: set[str] = set()
    out: list[str] = []

    def _add(nid: str):
        if nid not in seen and nid in nodes_by_id:
            seen.add(nid)
            out.append(nid)

    # 1. Exact node id (rare but precise)
    if q in nodes_by_id:
        _add(q)
    # 2. Exact source_file match
    for nid in file_index.get(q, []):
        _add(nid)
    # 3. Exact label match (case-insensitive)
    for nid in label_index.get(qlow, []):
        _add(nid)
    if len(out) >= limit:
        return out[:limit]
    # 4. Substring match in source_file
    for sf, nids in file_index.items():
        if qlow in sf.lower():
            for nid in nids:
                _add(nid)
                if len(out) >= limit:
                    return out
    # 5. Substring match in labels
    for lab, nids in label_index.items():
        if qlow in lab:
            for nid in nids:
                _add(nid)
                if len(out) >= limit:
                    return out
    return out[:limit]


def _summarise_node(n: dict) -> dict:
    """Strip noisy fields for caller-friendly payload."""
    return {
        "id": n.get("id"),
        "label": n.get("label"),
        "source_file": n.get("source_file"),
        "source_location": n.get("source_location"),
        "community": n.get("community"),
        "file_type": n.get("file_type"),
    }


def graphify_lookup(query: str, hops: int = 1,
                    max_matches: int = 8,
                    max_neighbors: int = 25,
                    repo_root: str | None = None) -> dict:
    """Look up nodes related to ``query`` in the graphify graph.

    Args:
      query: label (e.g. ``"Memory"``), source path
        (e.g. ``"aiforge_core/runtime/memory.py"``), or substring.
      hops: 1 (direct neighbours) or 2 (neighbours-of-neighbours, capped).
      max_matches: cap on resolved seed nodes.
      max_neighbors: cap on returned neighbour entries (per call, not per seed).
      repo_root: override search root. Falls back to ``AIFORGE_REPO_ROOT``
        env var, then walks up from this module.

    Returns:
      ``{ok, matches: [{id, label, source_file, ...}],
          neighbors: [{node, relation, weight, confidence, direction,
                       hop, via}]}`` on success.
      ``{ok: False, error}`` on failure.
    """
    try:
        root_path = _resolve_repo_root(repo_root)
        nodes_by_id, adj, file_index, label_index = _load(root_path)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"graphify graph load failed: {exc}"}

    if hops not in (1, 2):
        return {"ok": False, "error": f"hops must be 1 or 2, got {hops}"}

    seed_ids = _match_nodes(query, nodes_by_id, file_index, label_index,
                            max(1, max_matches))
    if not seed_ids:
        return {"ok": True, "matches": [], "neighbors": [],
                "note": "no matching nodes — try a label, file path, or substring"}

    neighbors: list[dict] = []
    seen_pairs: set[tuple[str, str, str]] = set()  # (src_id, tgt_id, relation)

    def _expand(src_id: str, hop: int, via: str | None):
        for edge in adj.get(src_id, []):
            tgt = edge["_to"]
            key = (src_id, tgt, edge["relation"])
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            tgt_node = nodes_by_id.get(tgt)
            if not tgt_node:
                continue
            neighbors.append({
                "node": _summarise_node(tgt_node),
                "relation": edge["relation"],
                "weight": edge["weight"],
                "confidence": edge["confidence"],
                "direction": edge["direction"],
                "hop": hop,
                "via": via,
                "source_location": edge["source_location"],
            })
            if len(neighbors) >= max_neighbors:
                return

    for sid in seed_ids:
        if len(neighbors) >= max_neighbors:
            break
        _expand(sid, 1, None)

    if hops == 2 and len(neighbors) < max_neighbors:
        # Snapshot first-hop targets so we don't mutate during iteration.
        first_hop_ids = [
            n["node"]["id"] for n in neighbors
            if n["hop"] == 1 and n["node"].get("id")
        ]
        for sid in first_hop_ids:
            if len(neighbors) >= max_neighbors:
                break
            _expand(sid, 2, sid)

    matches_payload = [_summarise_node(nodes_by_id[i]) for i in seed_ids]
    return {"ok": True, "matches": matches_payload, "neighbors": neighbors}


__all__ = ["graphify_lookup"]
