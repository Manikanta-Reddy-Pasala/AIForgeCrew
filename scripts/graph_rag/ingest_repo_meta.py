#!/usr/bin/env python3
"""Ingest repo_meta.py JSONL output into Neo4j (:Repo) node properties."""
from __future__ import annotations

import argparse
import json
import sys

from neo4j import GraphDatabase

UPSERT = """
UNWIND $rows AS r
MERGE (repo:Repo {name: r.repo})
SET repo.path = r.path,
    repo.lang = r.lang,
    repo.jdk = r.jdk,
    repo.build_tool = r.build.tool,
    repo.build_install = r.build.install,
    repo.build_test = r.test.run,
    repo.build_package = r.build.package,
    repo.build_run_local = r.build.run_local,
    repo.test_frameworks = r.test.frameworks,
    repo.commons_deps = r.commons_deps,
    repo.image_prefix = r.image_prefix,
    repo.env_required = r.env_required,
    repo.depends_on = r.depends_on,
    repo.meta_json = r.meta_json
"""


def flatten(rec: dict) -> dict:
    # Neo4j rejects nested maps; stash as meta_json, surface top-level keys.
    return {
        "repo": rec["repo"],
        "path": rec.get("path"),
        "lang": rec.get("lang"),
        "jdk": rec.get("jdk"),
        "build": rec.get("build") or {},
        "test": rec.get("test") or {},
        "commons_deps": rec.get("commons_deps") or [],
        "image_prefix": rec.get("image_prefix"),
        "env_required": rec.get("env_required") or [],
        "depends_on": rec.get("depends_on") or [],
        "meta_json": json.dumps(rec),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--neo4j", default="bolt://127.0.0.1:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    args = ap.parse_args()

    rows = []
    for path in args.files:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(flatten(json.loads(line)))

    drv = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))
    with drv.session() as s:
        s.run("CREATE CONSTRAINT repo_name IF NOT EXISTS "
              "FOR (r:Repo) REQUIRE r.name IS UNIQUE")
        for i in range(0, len(rows), 100):
            s.run(UPSERT, rows=rows[i:i + 100])
    drv.close()
    print(f"ingested {len(rows)} repos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
