#!/usr/bin/env python3
"""Blast-radius query. Given a Method fqn (or Symbol id), return every
entity impacted by a change: callers, data readers, subscribers, tests,
deployments, repos to rebuild.

Also available as MCP tool `impact`; this CLI is for validation.
"""
from __future__ import annotations

import argparse
import json
import sys

from neo4j import GraphDatabase

CYPHER = """
MATCH (m) WHERE (m:Method OR m:Function OR m:Symbol)
  AND (m.fqn=$key OR m.id=$key)
OPTIONAL MATCH (caller)-[:CALLS*1..$hops]->(m)
OPTIONAL MATCH (m)-[:WRITES|DELETES]->(coll:MongoCollection)<-[:READS]-(reader)
  WHERE reader <> m
OPTIONAL MATCH (m)-[:PUBLISH]->(subj:NatsSubject)<-[:SUBSCRIBE]-(sub)
  WHERE sub <> m
OPTIONAL MATCH (m)-[:EXPOSES]->(ep:Endpoint)<-[:CALLS_EXTERNAL]-(client)
OPTIONAL MATCH (t:Test)-[:TESTS]->(m)
OPTIONAL MATCH (m)<-[:CONTAINS|DEFINES*1..2]-(cls)<-[:HAS_FILE]-(repo:Repo)
OPTIONAL MATCH (repo)-[:IS_SERVICE]->(dep:Deployment)
OPTIONAL MATCH (m)-[:READS|WRITES|DELETES]->(coll2:MongoCollection)
OPTIONAL MATCH (m)-[:PUBLISH|SUBSCRIBE]->(subj2:NatsSubject)
OPTIONAL MATCH (m)-[:CALLS_EXTERNAL]->(ex:ExternalEndpoint)
RETURN {
  target: m.fqn,
  direct_callers: [x IN collect(DISTINCT caller) | x.fqn][0..20],
  data_readers:   [x IN collect(DISTINCT reader) | {fqn:x.fqn}][0..20],
  subscribers:    [x IN collect(DISTINCT sub) | {fqn:x.fqn}][0..20],
  http_clients:   [x IN collect(DISTINCT client) | x.fqn][0..20],
  tests:          [x IN collect(DISTINCT t) | x.name][0..30],
  deployments:    [x IN collect(DISTINCT dep) | {ns:x.ns, name:x.name, env:x.env}],
  repos:          [x IN collect(DISTINCT repo) | x.name],
  collections:    [x IN collect(DISTINCT coll2) | x.name],
  subjects:       [x IN collect(DISTINCT subj2) | x.subject],
  external_urls:  [x IN collect(DISTINCT ex) | x.url]
} AS impact
"""


def compute(driver, key: str, hops: int = 3) -> dict:
    with driver.session() as s:
        # parameterize depth by string concat since Cypher disallows $var in *n..m
        q = CYPHER.replace("$hops", str(hops))
        rec = s.run(q, key=key).single()
        return rec["impact"] if rec else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="Method fqn or Symbol id")
    ap.add_argument("--hops", type=int, default=3)
    ap.add_argument("--neo4j", default="bolt://127.0.0.1:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    args = ap.parse_args()

    drv = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))
    out = compute(drv, args.target, args.hops)
    drv.close()
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
