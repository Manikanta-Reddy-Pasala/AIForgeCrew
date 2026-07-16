#!/usr/bin/env python3
"""Graph-RAG ingester v2 — richer Java-aware graph.

Targets "claude-code" style context retrieval: import-aware call resolution,
method bodies + javadoc, layer tagging, Mongo R/W edges, REST-client
cross-service edges, Spring annotation flags. One-shot batched Neo4j writes.

Schema:
  (:Package {name})
  (:Class  {fqn, simple, kind, file, package, layer, loc, annotations,
            imports, javadoc, transactional, async, scheduled, cacheable})
  (:Method {fqn, name, sig, file, line, loc, return_type, body_snippet,
            javadoc, annotations, transactional, async, scheduled, cacheable})
  (:Endpoint {http, path, method_fqn, params})
  (:MongoCollection {name})
  (:NatsSubject {subject})
  (:ExternalEndpoint {url})     # cross-service REST target

  (Package)-[:CONTAINS_CLASS]->(Class)
  (Class)-[:CONTAINS]->(Method)
  (Class)-[:EXTENDS]->(Class)
  (Class)-[:IMPLEMENTS]->(Class)
  (Class)-[:USES {field}]->(Class)
  (Class)-[:IMPORTS]->(Class)
  (Class)-[:BINDS]->(MongoCollection)
  (Class)-[:PUBLISHES]->(NatsSubject)

  (Method)-[:EXPOSES]->(Endpoint)
  (Method)-[:CALLS {via, certainty}]->(Method)
  (Method)-[:READS|WRITES|DELETES]->(MongoCollection)
  (Method)-[:CALLS_EXTERNAL]->(ExternalEndpoint)

Usage:
    python ingest_java.py --repo <path> --reset
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from neo4j import GraphDatabase

from ingest_java_model import ClassInfo, MethodInfo
from ingest_java_parse import index_collection_hints, walk_repo
from ingest_java_graph import write_graph


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--neo4j", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"repo not found: {repo}", file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))

    if args.reset:
        with driver.session() as s:
            s.run("MATCH (n) DETACH DELETE n")
            print("reset")

    print("pass 1: indexing @Document collection hints ...")
    collection_hints = index_collection_hints(repo)
    print(f"  {len(collection_hints)} class→collection mappings")

    all_classes: list[ClassInfo] = []
    all_methods: list[MethodInfo] = []
    count = 0
    for rel, classes, methods in walk_repo(repo, collection_hints):
        all_classes.extend(classes)
        all_methods.extend(methods)
        count += 1
        if count % 100 == 0:
            print(f"... parsed {count} files")
    print(f"parsed {count} files | {len(all_classes)} classes | "
          f"{len(all_methods)} methods")

    write_graph(driver, all_classes, all_methods)

    with driver.session() as s:
        print("\n--- node counts ---")
        for l, c in s.run(
            "MATCH (n) RETURN labels(n)[0] AS l, count(*) AS c ORDER BY c DESC"
        ).values():
            print(f"  {l}: {c}")
        print("\n--- relationship counts ---")
        for typ, c in s.run(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY c DESC"
        ).values():
            print(f"  {typ}: {c}")
    driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
