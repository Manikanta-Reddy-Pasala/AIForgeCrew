#!/usr/bin/env python3
"""Hybrid semantic + structural query.

Ask a natural-language question, embed it with the same model used by
embed_nodes.py, find the top-K most similar Methods / Classes via the
Neo4j native vector index, then expand along CALLS / USES / EXPOSES etc.
so the result is a useful code neighbourhood, not just an isolated hit.

Usage:
    scripts/graph_rag/semantic.py "pagination for query endpoint"
    scripts/graph_rag/semantic.py --hops 2 --topk 5 "stock transfer workflow"
"""
from __future__ import annotations

import argparse
import sys

import httpx
from neo4j import GraphDatabase


def embed(base: str, model: str, text: str, api_key: str = "lm-studio") -> list[float]:
    resp = httpx.post(
        f"{base}/embeddings",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={"model": model, "input": text},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


METHOD_NEIGHBOURHOOD = """\
CALL db.index.vector.queryNodes('method_embedding_vec', $topk, $vec)
YIELD node AS m, score
MATCH (c:Class)-[:CONTAINS]->(m)
OPTIONAL MATCH (m)-[:CALLS*1..$hops]->(t:Method)
OPTIONAL MATCH (m)-[:EXPOSES]->(e:Endpoint)
OPTIONAL MATCH (m)-[r:READS|WRITES|DELETES]->(coll:MongoCollection)
RETURN score, c.simple AS class, c.layer AS layer, m.name AS method,
       m.file AS file, m.line AS line, m.return_type AS return_type,
       substring(m.javadoc, 0, 300) AS javadoc,
       substring(m.body_snippet, 0, 600) AS body,
       collect(DISTINCT (e.http + ' ' + e.path)) AS endpoints,
       collect(DISTINCT (type(r) + ' ' + coll.name)) AS mongo_ops,
       collect(DISTINCT t.fqn)[..25] AS reachable_methods
ORDER BY score DESC
"""

CLASS_NEIGHBOURHOOD = """\
CALL db.index.vector.queryNodes('class_embedding_vec', $topk, $vec)
YIELD node AS c, score
OPTIONAL MATCH (c)-[:CONTAINS]->(m:Method)-[:EXPOSES]->(e:Endpoint)
OPTIONAL MATCH (c)-[u:USES]->(dep:Class)
RETURN score, c.simple AS class, c.layer AS layer, c.package AS package,
       c.loc AS loc,
       substring(c.javadoc, 0, 300) AS javadoc,
       collect(DISTINCT (e.http + ' ' + e.path))[..10] AS endpoints,
       collect(DISTINCT dep.fqn)[..15] AS uses
ORDER BY score DESC
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="+")
    ap.add_argument("--neo4j", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    ap.add_argument("--lm", default="http://127.0.0.1:1235/v1")
    ap.add_argument("--model", default="text-embedding-nomic-embed-text-v1.5")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--hops", type=int, default=2,
                    help="how many CALLS hops to expand")
    ap.add_argument("--kind", choices=["method", "class", "both"], default="both")
    args = ap.parse_args()
    question = " ".join(args.question).strip()

    vec = embed(args.lm, args.model, question)

    drv = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))
    with drv.session() as s:
        if args.kind in ("method", "both"):
            print(f"\n=== top {args.topk} methods for: {question!r}")
            # hops is a plain int, inject into cypher string because
            # variable-length path lengths can't be parameterised.
            cypher = METHOD_NEIGHBOURHOOD.replace("$hops", str(args.hops))
            for r in s.run(cypher, vec=vec, topk=args.topk):
                d = dict(r)
                print(f"\n  [{d['score']:.3f}] {d['class']}.{d['method']}  "
                      f"({d['layer']}, L{d['line']}, "
                      f"returns {d['return_type']})")
                if d.get("endpoints"):
                    eps = [e for e in d['endpoints'] if e.strip()]
                    if eps:
                        print(f"    exposes: {eps}")
                if d.get("mongo_ops"):
                    mo = [m for m in d['mongo_ops'] if m.strip()]
                    if mo:
                        print(f"    mongo:   {mo}")
                jd = d.get("javadoc") or ""
                if jd.strip():
                    print(f"    javadoc: {jd.strip()[:200]}")
                body = (d.get("body") or "").strip()
                if body:
                    preview = body.splitlines()[0:3]
                    print("    body:    " + " \\n ".join(preview))
                reach = d.get("reachable_methods") or []
                if reach:
                    print(f"    reaches: {reach[:6]} (+{max(0,len(reach)-6)} more)")
        if args.kind in ("class", "both"):
            print(f"\n=== top {args.topk} classes for: {question!r}")
            for r in s.run(CLASS_NEIGHBOURHOOD, vec=vec, topk=args.topk):
                d = dict(r)
                print(f"\n  [{d['score']:.3f}] {d['class']}  "
                      f"({d['layer']}, L={d['loc']}, pkg={d['package']})")
                jd = d.get("javadoc") or ""
                if jd.strip():
                    print(f"    javadoc: {jd.strip()[:200]}")
                eps = [e for e in (d.get("endpoints") or []) if e.strip()]
                if eps:
                    print(f"    exposes: {eps}")
                uses = d.get("uses") or []
                if uses:
                    print(f"    uses:    {uses}")
    drv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
