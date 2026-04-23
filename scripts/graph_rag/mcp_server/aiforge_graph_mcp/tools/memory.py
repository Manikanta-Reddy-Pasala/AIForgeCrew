"""Memory + docs tools: search claude-memory notes + CLAUDE.md text."""
from __future__ import annotations

from ..cypher_lib import session, embed, VECTOR_QUERY, FULLTEXT_QUERY


def find_doc(args: dict) -> dict:
    q = args["query"]
    k = int(args.get("k", 8))
    vec = embed(q)
    hits: list[dict] = []
    with session() as s:
        try:
            for r in s.run(VECTOR_QUERY, index="memory_embedding_vec", k=k, vec=vec):
                hits.append({"src": "vec", "score": r["score"], "node": dict(r["node"])})
        except Exception:
            pass
        try:
            for r in s.run(FULLTEXT_QUERY, index="memory_text", q=q, k=k):
                hits.append({"src": "bm25", "score": r["score"], "node": dict(r["node"])})
        except Exception:
            pass
    by_path: dict[str, dict] = {}
    for h in hits:
        p = h["node"].get("path")
        if p and (p not in by_path or by_path[p]["score"] < h["score"]):
            by_path[p] = h
    out = sorted(by_path.values(), key=lambda x: x["score"], reverse=True)[:k]
    return {"hits": out}


def related_memories(args: dict) -> dict:
    """For a given node (repo/method/collection/subject/endpoint) return
    memories that DESCRIBE it."""
    key = args["key"]
    cy = """
    MATCH (t) WHERE t.name=$key OR t.fqn=$key OR t.path=$key
                 OR t.subject=$key OR t.id=$key
    MATCH (m:Memory)-[:DESCRIBES]->(t)
    RETURN DISTINCT m.path AS path, m.title AS title, m.type AS type,
           m.description AS description
    LIMIT 20
    """
    with session() as s:
        return {"memories": [dict(r) for r in s.run(cy, key=key)]}


TOOLS = [
    {
        "name": "find_doc",
        "description": "Semantic + BM25 search over Claude memories, runbooks, design notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "related_memories",
        "description": "Memories that explicitly describe a repo/method/collection/subject/endpoint.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
]

HANDLERS = {
    "find_doc": find_doc,
    "related_memories": related_memories,
}
