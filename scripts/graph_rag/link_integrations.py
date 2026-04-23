#!/usr/bin/env python3
"""Post-ingest pass: create cross-repo flow edges from integration nodes.

Runs after all repos have been ingested with ingest_jsonl.py which already
emits (:Method)-[:PUBLISH|SUBSCRIBE]->(:NatsSubject) etc. This script joins
producer <-> consumer and emits FLOWS_TO edges.
"""
from __future__ import annotations

import argparse
import sys

from neo4j import GraphDatabase


# Each tuple: name, cypher, expect-count logging label
PASSES = [
    ("nats_flows", """
        MATCH (p:Method)-[:PUBLISH]->(s:NatsSubject)
        MATCH (s)<-[:SUBSCRIBE]-(c:Method)
        WHERE p.fqn <> c.fqn
        MERGE (p)-[r:FLOWS_TO {via:'nats'}]->(c)
        SET r.subject = s.subject
        RETURN count(*) AS n
    """),
    ("http_flows", """
        // Use endsWith/startsWith instead of CONTAINS to avoid range-index
        // STRING_CONTAINS predicate error on Neo4j 5 with the endpoint_path
        // index. Keeps the semantic: external URL hits the endpoint path.
        MATCH (caller:Method)-[:CALLS_EXTERNAL]->(ext:ExternalEndpoint)
        WITH caller, ext, ext.url AS url
        MATCH (handler:Method)-[:EXPOSES]->(e:Endpoint)
        WHERE url ENDS WITH e.path OR url STARTS WITH e.path
        MERGE (caller)-[r:FLOWS_TO {via:'http'}]->(handler)
        SET r.path = e.path
        RETURN count(*) AS n
    """),
    ("mongo_data_flows", """
        MATCH (w:Method)-[:WRITES]->(c:MongoCollection)
        MATCH (r:Method)-[:READS]->(c)
        WHERE w.fqn <> r.fqn
        MERGE (w)-[rel:DATA_FLOWS_TO {via:'mongo'}]->(r)
        SET rel.collection = c.name
        RETURN count(*) AS n
    """),
    ("mongo_delete_flows", """
        MATCH (d:Method)-[:DELETES]->(c:MongoCollection)
        MATCH (r:Method)-[:READS]->(c)
        WHERE d.fqn <> r.fqn
        MERGE (d)-[rel:DATA_FLOWS_TO {via:'mongo_delete'}]->(r)
        SET rel.collection = c.name
        RETURN count(*) AS n
    """),
    ("kafka_flows", """
        MATCH (p:Method)-[:PRODUCES]->(t:KafkaTopic)
        MATCH (t)<-[:CONSUMES]-(c:Method)
        MERGE (p)-[r:FLOWS_TO {via:'kafka'}]->(c)
        SET r.topic = t.name
        RETURN count(*) AS n
    """),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--neo4j", default="bolt://127.0.0.1:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    ap.add_argument("--only", nargs="*", help="run only selected passes by name")
    args = ap.parse_args()

    drv = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))
    with drv.session() as s:
        for name, cy in PASSES:
            if args.only and name not in args.only:
                continue
            try:
                rec = s.run(cy).single()
                n = rec["n"] if rec else 0
                print(f"{name}: {n} edges")
            except Exception as exc:
                print(f"{name}: skipped ({exc})", file=sys.stderr)
    drv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
