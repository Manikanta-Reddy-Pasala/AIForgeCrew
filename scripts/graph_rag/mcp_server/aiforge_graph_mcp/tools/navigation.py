"""Navigation tools: graph neighborhood, caller/callee chains, source read."""
from __future__ import annotations

from pathlib import Path

from ..cypher_lib import session, NEIGHBOR_CY


def graph_neighborhood(args: dict) -> dict:
    key = args["key"]
    rels = args.get("rels") or []
    limit = int(args.get("limit", 30))
    with session() as s:
        r = s.run(NEIGHBOR_CY, nid=-1, key=key, rels=rels, limit=limit).single()
        if not r:
            return {"error": "not found"}
        return {
            "node": dict(r["n"]),
            "outgoing": [{"rel": e["rel"], "node": dict(e["node"]), "dir": e["dir"]}
                         for e in r["outgoing"] if e["node"]],
            "incoming": [{"rel": e["rel"], "node": dict(e["node"]), "dir": e["dir"]}
                         for e in r["incoming"] if e["node"]],
        }


def caller_chain(args: dict) -> dict:
    key = args["key"]
    depth = int(args.get("depth", 3))
    cy = f"""
    MATCH (m) WHERE m.fqn=$key OR m.id=$key
    MATCH p=(caller)-[:CALLS*1..{depth}]->(m)
    RETURN [n IN nodes(p) | {{fqn: coalesce(n.fqn,n.id,n.name), file: n.file}}] AS chain
    LIMIT 50
    """
    with session() as s:
        return {"chains": [r["chain"] for r in s.run(cy, key=key)]}


def callee_chain(args: dict) -> dict:
    key = args["key"]
    depth = int(args.get("depth", 3))
    cy = f"""
    MATCH (m) WHERE m.fqn=$key OR m.id=$key
    MATCH p=(m)-[:CALLS*1..{depth}]->(callee)
    RETURN [n IN nodes(p) | {{fqn: coalesce(n.fqn,n.id,n.name), file: n.file}}] AS chain
    LIMIT 50
    """
    with session() as s:
        return {"chains": [r["chain"] for r in s.run(cy, key=key)]}


def read_source(args: dict) -> dict:
    path = args["path"]
    start = int(args.get("start_line", 1))
    end = int(args.get("end_line", 0))
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"file not found: {path}"}
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    if end <= 0 or end > len(lines):
        end = min(start + 200, len(lines))
    slice_ = lines[max(0, start - 1):end]
    return {
        "path": str(p),
        "start_line": start,
        "end_line": end,
        "content": "\n".join(slice_),
    }


TOOLS = [
    {
        "name": "graph_neighborhood",
        "description": "Fetch 1-hop in/out neighbors for a node (by fqn or id). "
                       "Filter by relationship types via `rels` param.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "rels": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "default": 30},
            },
            "required": ["key"],
        },
    },
    {
        "name": "caller_chain",
        "description": "Upstream callers of a method/function (depth-limited).",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "depth": {"type": "integer", "default": 3},
            },
            "required": ["key"],
        },
    },
    {
        "name": "callee_chain",
        "description": "Downstream callees from a method/function (depth-limited).",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "depth": {"type": "integer", "default": 3},
            },
            "required": ["key"],
        },
    },
    {
        "name": "read_source",
        "description": "Read an exact file slice from disk (start_line..end_line).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "default": 1},
                "end_line": {"type": "integer", "default": 0},
            },
            "required": ["path"],
        },
    },
]

HANDLERS = {
    "graph_neighborhood": graph_neighborhood,
    "caller_chain": caller_chain,
    "callee_chain": callee_chain,
    "read_source": read_source,
}
