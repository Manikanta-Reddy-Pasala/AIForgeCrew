"""Discovery tools: semantic + fulltext hybrid search, listing helpers."""
from __future__ import annotations

from ..cypher_lib import session, embed, rerank, VECTOR_QUERY, FULLTEXT_QUERY


def sym_lookup(args: dict) -> dict:
    q = args["query"]
    repo = args.get("repo")
    kind = args.get("kind")
    k = int(args.get("k", 10))

    vec = embed(q)
    hits: list[dict] = []
    with session() as s:
        for idx in ("method_embedding_vec", "endpoint_embedding_vec", "memory_embedding_vec"):
            try:
                for r in s.run(VECTOR_QUERY, index=idx, k=k * 2, vec=vec):
                    n = dict(r["node"])
                    hits.append({"src": "vec", "index": idx, "score": r["score"], "node": n})
            except Exception:
                continue
        try:
            for r in s.run(FULLTEXT_QUERY, index="method_text", q=q, k=k * 2):
                hits.append({"src": "bm25", "score": r["score"], "node": dict(r["node"])})
        except Exception:
            pass

    if repo:
        hits = [h for h in hits if (h["node"].get("repo") == repo
                                    or h["node"].get("package", "").startswith(repo))]
    if kind:
        hits = [h for h in hits if h["node"].get("kind") == kind]

    # Deduplicate by fqn/path, keep best vector score as primary.
    seen: dict[str, dict] = {}
    for h in hits:
        key = h["node"].get("fqn") or h["node"].get("id") or h["node"].get("path") or \
              h["node"].get("name")
        if key and (key not in seen or seen[key]["score"] < h["score"]):
            seen[key] = h

    ranked = sorted(seen.values(), key=lambda h: h["score"], reverse=True)[:k * 2]

    # Optional reranker compress
    try:
        texts = [
            (h["node"].get("sig") or h["node"].get("signature") or
             h["node"].get("title") or h["node"].get("fqn") or "")
            + "\n" + (h["node"].get("doc") or h["node"].get("javadoc")
                      or h["node"].get("body") or h["node"].get("description") or "")[:800]
            for h in ranked
        ]
        scores = rerank(q, texts)
        for h, sc in zip(ranked, scores):
            h["rerank"] = sc
        ranked.sort(key=lambda h: h.get("rerank", 0), reverse=True)
    except Exception:
        pass
    return {"hits": ranked[:k]}


def list_repos(args: dict) -> dict:
    lang = args.get("lang")
    cy = "MATCH (r:Repo) "
    params: dict = {}
    if lang:
        cy += "WHERE r.lang = $lang "
        params["lang"] = lang
    cy += "RETURN r ORDER BY r.name"
    with session() as s:
        return {"repos": [dict(r["r"]) for r in s.run(cy, **params)]}


def list_services(args: dict) -> dict:
    env = args.get("env")
    cy = (
        "MATCH (r:Repo)-[:IS_SERVICE]->(d:Deployment) "
        + (f"WHERE d.env_label = $env " if env else "")
        + "RETURN r.name AS repo, d.cluster AS cluster, d.ns AS ns, "
          "d.name AS deploy, d.image AS image, d.env_label AS env "
          "ORDER BY repo"
    )
    with session() as s:
        return {"services": [dict(r) for r in s.run(cy, env=env)]}


def list_endpoints(args: dict) -> dict:
    repo = args.get("repo")
    pattern = args.get("pattern")
    cy = "MATCH (e:Endpoint) "
    cond = []
    params: dict = {}
    if repo:
        cy = "MATCH (r:Repo {name:$repo})-[:HAS_FILE]->(:File)-[:DEFINES]->(:Class)-[:CONTAINS]->(m:Method)-[:EXPOSES]->(e:Endpoint) "
        params["repo"] = repo
    if pattern:
        cond.append("e.path CONTAINS $pat")
        params["pat"] = pattern
    if cond:
        cy += "WHERE " + " AND ".join(cond) + " "
    cy += "RETURN DISTINCT e.http AS http, e.path AS path ORDER BY path LIMIT 200"
    with session() as s:
        return {"endpoints": [dict(r) for r in s.run(cy, **params)]}


def list_integrations(args: dict) -> dict:
    kind = args.get("kind")
    repo = args.get("repo")
    label = {
        "mongo": "MongoCollection",
        "nats": "NatsSubject",
        "redis": "RedisKey",
        "kafka": "KafkaTopic",
        "external": "ExternalEndpoint",
    }.get(kind, "MongoCollection")
    cy = f"MATCH (x:{label}) RETURN x LIMIT 500"
    with session() as s:
        return {"kind": kind, "items": [dict(r["x"]) for r in s.run(cy)]}


TOOLS = [
    {
        "name": "sym_lookup",
        "description": (
            "Hybrid BM25+vector+rerank search over Methods, Endpoints, Memories. "
            "Returns ranked candidates with fqn/file/score."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "repo": {"type": "string"},
                "kind": {"type": "string"},
                "k": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_repos",
        "description": "List all indexed repos. Optional lang filter (java|python|node|react).",
        "input_schema": {"type": "object", "properties": {"lang": {"type": "string"}}},
    },
    {
        "name": "list_services",
        "description": "List repo <-> k8s deployment bindings. Optional env filter (qa|prod).",
        "input_schema": {"type": "object", "properties": {"env": {"type": "string"}}},
    },
    {
        "name": "list_endpoints",
        "description": "List REST endpoints, optionally filtered by repo or path pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}, "pattern": {"type": "string"}},
        },
    },
    {
        "name": "list_integrations",
        "description": "List Mongo collections / NATS subjects / Redis keys / Kafka topics.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["mongo", "nats", "redis", "kafka", "external"]},
                "repo": {"type": "string"},
            },
        },
    },
]

HANDLERS = {
    "sym_lookup": sym_lookup,
    "list_repos": list_repos,
    "list_services": list_services,
    "list_endpoints": list_endpoints,
    "list_integrations": list_integrations,
}
