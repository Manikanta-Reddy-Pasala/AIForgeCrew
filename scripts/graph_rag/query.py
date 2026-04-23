#!/usr/bin/env python3
"""Graph-RAG query helper — answer code-navigation questions against the
Neo4j graph produced by ingest_java.py v2.
"""
from __future__ import annotations

import argparse
import json
import sys

from neo4j import GraphDatabase


QUERIES = {
    "classes": (
        "classes whose name mentions the topic (layer + LOC + flags)",
        "MATCH (c:Class) WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "RETURN c.simple AS name, c.layer AS layer, c.kind AS kind, "
        "c.loc AS loc, c.transactional AS tx, c.async AS async, "
        "c.package AS pkg, c.file AS file ORDER BY c.simple",
    ),
    "endpoints": (
        "REST endpoints exposing the topic",
        "MATCH (m:Method)-[:EXPOSES]->(e:Endpoint) "
        "WHERE toLower(e.path) CONTAINS toLower($topic) "
        "RETURN e.http AS http, e.path AS path, e.params AS params, "
        "m.fqn AS method ORDER BY e.path",
    ),
    "deps_out": (
        "outbound autowired deps of the topic's classes",
        "MATCH (c:Class)-[u:USES]->(dep:Class) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "RETURN c.simple AS class, u.field AS field, dep.fqn AS uses "
        "ORDER BY c.simple",
    ),
    "deps_in": (
        "inbound callers / wirers of the topic",
        "MATCH (src:Class)-[u:USES]->(tgt:Class) "
        "WHERE toLower(tgt.simple) CONTAINS toLower($topic) "
        "RETURN src.fqn AS caller, u.field AS field, tgt.simple AS target",
    ),
    "callgraph": (
        "resolved + unresolved call edges from the topic",
        "MATCH (c:Class)-[:CONTAINS]->(m:Method)-[r:CALLS]->(t:Method) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "RETURN DISTINCT c.simple AS class, m.name AS method, "
        "t.fqn AS calls, r.certainty AS cert, r.via AS via "
        "ORDER BY class, method LIMIT 200",
    ),
    "inbound_calls": (
        "methods reaching into the topic's classes",
        "MATCH (caller:Method)-[:CALLS]->(m:Method)<-[:CONTAINS]-(c:Class) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "RETURN caller.fqn AS caller, c.simple AS into_class, m.name "
        "AS method ORDER BY caller LIMIT 50",
    ),
    "mongo_rw": (
        "mongo reads / writes / deletes by topic methods",
        "MATCH (c:Class)-[:CONTAINS]->(m:Method)-[r]->(coll:MongoCollection) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) AND "
        "type(r) IN ['READS','WRITES','DELETES'] "
        "RETURN c.simple AS class, m.name AS method, type(r) AS op, "
        "coll.name AS collection ORDER BY class, method",
    ),
    "external": (
        "external REST / WebClient targets called by topic methods",
        "MATCH (c:Class)-[:CONTAINS]->(m:Method)-[:CALLS_EXTERNAL]->(e:ExternalEndpoint) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "RETURN c.simple AS class, m.name AS method, e.url AS url",
    ),
    "nats": (
        "NATS publish / subscribe subjects touched by topic",
        "MATCH (c:Class)-[:CONTAINS]->(m:Method)-[r]->(s:NatsSubject) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) AND "
        "type(r) IN ['PUBLISH','SUBSCRIBE'] "
        "RETURN c.simple AS class, m.name AS method, type(r) AS op, "
        "s.subject AS subject",
    ),
    "cross_service": (
        "classes in the topic that autowire cross-service clients",
        "MATCH (c:Class)-[:USES]->(dep:Class) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "AND (dep.fqn CONTAINS 'BackendService' OR dep.fqn CONTAINS 'WebClient' "
        "OR dep.fqn CONTAINS 'FeignClient' OR dep.fqn CONTAINS 'MongoTemplate' "
        "OR dep.fqn CONTAINS 'Nats' OR dep.fqn CONTAINS 'SyncService') "
        "RETURN c.simple AS class, dep.fqn AS depends_on",
    ),
    "fanout": (
        "2-hop fan-out reach from each topic method (triage signal)",
        "MATCH (c:Class)-[:CONTAINS]->(m:Method)-[:CALLS*1..2]->(t:Method) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "RETURN c.simple AS class, m.name AS entry, "
        "count(DISTINCT t) AS reachable ORDER BY reachable DESC LIMIT 15",
    ),
    "ctx_brief": (
        "javadoc + body snippet per topic method (LLM context brief)",
        "MATCH (c:Class)-[:CONTAINS]->(m:Method) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "RETURN c.simple AS class, m.name AS method, m.return_type AS rt, "
        "m.javadoc AS javadoc, substring(m.body_snippet, 0, 600) AS body "
        "ORDER BY c.simple, m.line LIMIT 25",
    ),
    "transactional": (
        "@Transactional / @Async / @Scheduled methods in topic",
        "MATCH (c:Class)-[:CONTAINS]->(m:Method) "
        "WHERE toLower(c.simple) CONTAINS toLower($topic) "
        "AND (m.transactional = true OR m.async = true OR m.scheduled = true) "
        "RETURN c.simple, m.name, m.transactional AS tx, m.async AS async, "
        "m.scheduled AS scheduled",
    ),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="stockTransfer")
    ap.add_argument("--neo4j", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    ap.add_argument("--only", nargs="+", choices=list(QUERIES))
    ap.add_argument("--free", help="raw Cypher; $topic is bound")
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
