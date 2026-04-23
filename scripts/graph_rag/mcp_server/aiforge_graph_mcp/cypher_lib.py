"""Shared Cypher templates + Neo4j session helpers."""
from __future__ import annotations

import os
from contextlib import contextmanager

import httpx
from neo4j import GraphDatabase


NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASS", "password")
# Default: LM Studio OpenAI-compatible endpoint reachable on NUC
# localhost:1235 via the lm-tunnel systemd unit (forwards to Mac Studio
# :1234). Override via EMBED_URL / EMBED_MODEL.
EMBED_URL = os.environ.get("EMBED_URL", "http://127.0.0.1:1235/v1")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")
# LM Studio does not serve a reranker. Default blank -> rerank() returns
# identity scores and caller keeps vector-order ranking.
RERANK_URL = os.environ.get("RERANK_URL", "")
# LLM (used by ticket_brief for summary if needed). Same tunnel.
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:1235/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3-coder-next")

_driver = None


def driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    return _driver


@contextmanager
def session():
    with driver().session() as s:
        yield s


def embed(text: str) -> list[float]:
    """Call OpenAI /v1/embeddings (LM Studio compatible) first, fall back to
    TEI /embed if that fails. Both shapes handled."""
    try:
        r = httpx.post(
            f"{EMBED_URL}/embeddings",
            json={"model": EMBED_MODEL, "input": text},
            headers={"Authorization": "Bearer lm-studio"},
            timeout=30,
        )
        r.raise_for_status()
        j = r.json()
        if isinstance(j, dict) and "data" in j:
            return j["data"][0]["embedding"]
    except Exception:
        pass
    r = httpx.post(f"{EMBED_URL}/embed", json={"inputs": text}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if isinstance(j, list) and j and isinstance(j[0], list):
        return j[0]
    return j


def rerank(query: str, texts: list[str]) -> list[float]:
    """Optional reranker. If RERANK_URL is empty or unreachable, return
    uniform zero scores — callers keep their vector-derived order."""
    if not texts or not RERANK_URL:
        return [0.0] * len(texts)
    try:
        r = httpx.post(f"{RERANK_URL}/rerank",
                       json={"query": query, "texts": texts}, timeout=30)
        r.raise_for_status()
        out = r.json()
        scores = [0.0] * len(texts)
        for item in out:
            scores[item["index"]] = item["score"]
        return scores
    except Exception:
        return [0.0] * len(texts)


# Common Cypher snippets

VECTOR_QUERY = """
CALL db.index.vector.queryNodes($index, $k, $vec)
YIELD node, score
RETURN node, score
"""

FULLTEXT_QUERY = """
CALL db.index.fulltext.queryNodes($index, $q)
YIELD node, score
RETURN node, score LIMIT $k
"""

IMPACT_CY = """
MATCH (m) WHERE (m:Method OR m:Function OR m:Symbol)
  AND (m.fqn=$key OR m.id=$key)
OPTIONAL MATCH (caller)-[:CALLS*1..3]->(m)
OPTIONAL MATCH (m)-[:WRITES|DELETES]->(c:MongoCollection)<-[:READS]-(reader)
  WHERE reader <> m
OPTIONAL MATCH (m)-[:PUBLISH]->(s:NatsSubject)<-[:SUBSCRIBE]-(sub)
  WHERE sub <> m
OPTIONAL MATCH (m)-[:EXPOSES]->(e:Endpoint)<-[:CALLS_EXTERNAL]-(cl)
OPTIONAL MATCH (t:Test)-[:TESTS]->(m)
OPTIONAL MATCH (m)-[:READS|WRITES|DELETES]->(c2:MongoCollection)
OPTIONAL MATCH (m)-[:PUBLISH|SUBSCRIBE]->(s2:NatsSubject)
OPTIONAL MATCH (m)-[:CALLS_EXTERNAL]->(ex:ExternalEndpoint)
RETURN {
  target: m.fqn,
  callers:       [x IN collect(DISTINCT caller) | x.fqn][0..20],
  data_readers:  [x IN collect(DISTINCT reader) | x.fqn][0..20],
  subscribers:   [x IN collect(DISTINCT sub) | x.fqn][0..20],
  http_clients:  [x IN collect(DISTINCT cl) | x.fqn][0..20],
  tests:         [x IN collect(DISTINCT t) | t.name][0..30],
  collections:   [x IN collect(DISTINCT c2) | c2.name],
  subjects:      [x IN collect(DISTINCT s2) | s2.subject],
  external_urls: [x IN collect(DISTINCT ex) | ex.url]
} AS impact
"""

NEIGHBOR_CY = """
MATCH (n) WHERE id(n) = $nid OR n.fqn = $key OR n.id = $key
OPTIONAL MATCH (n)-[r]->(out) WHERE type(r) IN $rels OR size($rels)=0
OPTIONAL MATCH (in)-[r2]->(n) WHERE type(r2) IN $rels OR size($rels)=0
RETURN n,
       collect(DISTINCT {rel:type(r), node:out, dir:'out'})[0..$limit] AS outgoing,
       collect(DISTINCT {rel:type(r2), node:in, dir:'in'})[0..$limit] AS incoming
"""
