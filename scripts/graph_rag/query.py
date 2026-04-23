#!/usr/bin/env python3
"""Graph-RAG query helper — answer "what is the full stock transfer flow?" style
questions against the ingested Neo4j graph (see ingest_java.py).

Usage:
    python query.py --topic stockTransfer
    python query.py --free 'MATCH (e:Endpoint) WHERE e.path CONTAINS "stockTransfer" RETURN e LIMIT 10'
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from neo4j import GraphDatabase


QUERIES = {
    "endpoints": (
        "endpoints exposed for this topic",
        "MATCH (m:Method)-[:EXPOSES]->(e:Endpoint) "
        "WHERE toLower(e.path) CONTAINS toLower($topic) "
        "RETURN e.http AS http, e.path AS path, m.fqn AS method ORDER BY e.path",
    ),
    "classes": (
        "classes whose name mentions the topic",
        "MATCH (c:Class) WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "RETURN c.simple AS name, c.kind AS kind, c.package AS pkg, c.file AS file "
        "ORDER BY c.simple",
    ),
    "deps_out": (
        "outbound autowired dependencies of the topic's classes",
        "MATCH (c:Class)-[u:USES]->(dep:Class) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "RETURN c.simple AS class, u.field AS field, dep.fqn AS uses ORDER BY c.simple",
    ),
    "deps_in": (
        "classes that autowire one of the topic's classes",
        "MATCH (src:Class)-[u:USES]->(tgt:Class) "
        "WHERE toLower(tgt.simple) CONTAINS toLower($topic) "
        "RETURN src.fqn AS caller, u.field AS field, tgt.simple AS target",
    ),
    "callgraph": (
        "direct call edges from the topic's classes to other methods",
        "MATCH (c:Class)-[:CONTAINS]->(m:Method)-[:CALLS]->(t:Method) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "RETURN DISTINCT c.simple AS class, m.name AS method, t.fqn AS calls "
        "ORDER BY class, method LIMIT 100",
    ),
    "inbound_calls": (
        "methods that call into the topic's classes",
        "MATCH (caller:Method)-[:CALLS]->(m:Method)<-[:CONTAINS]-(c:Class) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "RETURN caller.fqn AS caller, c.simple AS into_class, m.name AS method "
        "ORDER BY caller LIMIT 50",
    ),
    "cross_service": (
        "classes in the topic that autowire known cross-service clients",
        "MATCH (c:Class)-[:USES]->(dep:Class) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "AND (dep.fqn CONTAINS 'BackendService' OR dep.fqn CONTAINS 'WebClient' "
        "OR dep.fqn CONTAINS 'FeignClient' OR dep.fqn CONTAINS 'MongoTemplate' "
        "OR dep.fqn CONTAINS 'Nats' OR dep.fqn CONTAINS 'SyncService') "
        "RETURN c.simple AS class, dep.fqn AS depends_on",
    ),
    "neighbours": (
        "2-hop neighbourhood from controller class methods",
        "MATCH (c:Class)-[:CONTAINS]->(m:Method)-[:CALLS*1..2]->(t:Method) "
        "WHERE c.simple CONTAINS 'Warehouse' AND m.name CONTAINS 'tockTransfer' "
        "RETURN DISTINCT m.name AS entry, t.fqn AS reaches LIMIT 80",
    ),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="stockTransfer",
                    help="substring used in class/endpoint path matching")
    ap.add_argument("--neo4j", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    ap.add_argument("--only", nargs="+", choices=list(QUERIES),
                    help="run only these canned queries")
    ap.add_argument("--free", help="run a raw Cypher query instead")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    drv = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))
    with drv.session() as s:
        if args.free:
            rows = [dict(r) for r in s.run(args.free, topic=args.topic)]
            print(json.dumps(rows, default=str, indent=2))
            drv.close()
            return 0

        keys = args.only or list(QUERIES)
        out: dict = {}
        for k in keys:
            desc, cypher = QUERIES[k]
            rows = [dict(r) for r in s.run(cypher, topic=args.topic)]
            if args.json:
                out[k] = {"desc": desc, "rows": rows}
            else:
                print(f"\n=== {k} — {desc} ({len(rows)} rows)")
                for r in rows:
                    print(" ", r)
        if args.json:
            print(json.dumps(out, default=str, indent=2))
    drv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
