#!/usr/bin/env python3
"""Bind (:Repo) to (:Deployment) via config/service-map.yaml, and link
Deployments to DockerImages they run.

Also creates (:Repo)-[:EMITS_IMAGE]->(:DockerImage) from image_prefix.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from neo4j import GraphDatabase

CFG = Path(__file__).parent / "config" / "service-map.yaml"


UPSERT_REPO = """
MERGE (r:Repo {name:$repo})
SET r.image_prefix = $image_prefix
"""

LINK_IMAGE = """
MERGE (i:DockerImage {repo: $image_prefix})
WITH i
MATCH (r:Repo {name:$repo})
MERGE (r)-[:EMITS_IMAGE]->(i)
"""

LINK_DEPLOY = """
MATCH (r:Repo {name:$repo})
MERGE (d:Deployment {cluster:$cluster, ns:$ns, name:$name})
SET d.env = $env
MERGE (r)-[rel:IS_SERVICE {env:$env}]->(d)
"""

DEPS_BETWEEN = """
UNWIND $deps AS dep
MATCH (r:Repo {name:$repo}), (other:Repo {name:dep})
MERGE (r)-[:DEPENDS_ON]->(other)
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--neo4j", default="bolt://127.0.0.1:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    args = ap.parse_args()

    cfg = yaml.safe_load(CFG.read_text()) or {}
    bindings = (cfg.get("bindings") or {})

    drv = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))
    with drv.session() as s:
        for repo, data in bindings.items():
            s.run(UPSERT_REPO, repo=repo, image_prefix=data.get("image_prefix"))
            if data.get("image_prefix"):
                s.run(LINK_IMAGE, repo=repo, image_prefix=data["image_prefix"])
            for env, d in (data.get("deployments") or {}).items():
                if not d:
                    continue
                s.run(LINK_DEPLOY, repo=repo,
                      cluster=env, ns=d["ns"], name=d["name"], env=env)
            if data.get("depends_on"):
                s.run(DEPS_BETWEEN, repo=repo, deps=data["depends_on"])
    drv.close()
    print(f"linked {len(bindings)} repos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
