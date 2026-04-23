#!/usr/bin/env python3
"""Scan (:Memory).body for code references and create DESCRIBES edges.

Patterns configured in config/link-patterns.yaml. Only creates an edge when
the named target already exists in the graph (prevents orphan edges).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml
from neo4j import GraphDatabase

CFG = Path(__file__).parent / "config" / "link-patterns.yaml"


TARGET_CYPHER = {
    "file_path": (
        "MATCH (m:Memory {path:$p}), (t:File {path:$target}) "
        "MERGE (m)-[r:DESCRIBES {kind:'file'}]->(t) SET r.confidence = $c"
    ),
    "java_fqn": (
        "MATCH (m:Memory {path:$p}) "
        "MATCH (t) WHERE (t:Method OR t:Class OR t:Symbol) AND "
        "(t.fqn=$target OR t.id=$target) "
        "MERGE (m)-[r:DESCRIBES {kind:'fqn'}]->(t) SET r.confidence = $c"
    ),
    "mongo_coll": (
        "MATCH (m:Memory {path:$p}), (t:MongoCollection {name:$target}) "
        "MERGE (m)-[r:DESCRIBES {kind:'mongo'}]->(t) SET r.confidence = $c"
    ),
    "nats_subject": (
        "MATCH (m:Memory {path:$p}), (t:NatsSubject {subject:$target}) "
        "MERGE (m)-[r:DESCRIBES {kind:'nats'}]->(t) SET r.confidence = $c"
    ),
    "endpoint": (
        "MATCH (m:Memory {path:$p}), (t:Endpoint {path:$target}) "
        "MERGE (m)-[r:DESCRIBES {kind:'rest'}]->(t) SET r.confidence = $c"
    ),
    "repo_name": (
        "MATCH (m:Memory {path:$p}), (t:Repo {name:$target}) "
        "MERGE (m)-[r:DESCRIBES {kind:'repo'}]->(t) SET r.confidence = $c"
    ),
    "redis_key": (
        "MATCH (m:Memory {path:$p}) "
        "MERGE (t:RedisKey {pattern:$target}) "
        "MERGE (m)-[r:DESCRIBES {kind:'redis'}]->(t) SET r.confidence = $c"
    ),
}


def compile_patterns(cfg: dict) -> dict[str, re.Pattern]:
    return {k: re.compile(v) for k, v in (cfg.get("patterns") or {}).items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--neo4j", default="bolt://127.0.0.1:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    ap.add_argument("--limit", type=int, default=0, help="process at most N memories (0=all)")
    args = ap.parse_args()

    cfg = yaml.safe_load(CFG.read_text())
    patterns = compile_patterns(cfg)
    conf = cfg.get("confidence") or {}

    drv = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))
    total_edges = 0
    with drv.session() as s:
        q = "MATCH (m:Memory) RETURN m.path AS path, m.body AS body"
        if args.limit:
            q += f" LIMIT {args.limit}"
        memories = list(s.run(q))
        for rec in memories:
            path, body = rec["path"], rec["body"] or ""
            seen: set[tuple[str, str]] = set()
            for kind, pat in patterns.items():
                if kind not in TARGET_CYPHER:
                    continue
                for m in pat.finditer(body):
                    target = m.group("target")
                    if (kind, target) in seen:
                        continue
                    seen.add((kind, target))
                    try:
                        s.run(TARGET_CYPHER[kind],
                              p=path, target=target,
                              c=float(conf.get(kind, 0.5)))
                        total_edges += 1
                    except Exception as exc:
                        print(f"  skip {kind}:{target} -> {exc}", file=sys.stderr)
    drv.close()
    print(f"DESCRIBES edges created/merged: {total_edges}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
