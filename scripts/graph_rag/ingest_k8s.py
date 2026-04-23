#!/usr/bin/env python3
"""Ingest k8s_sync.py JSONL output into Neo4j.

Produces:
  (:Cluster)-[:HAS_NS]->(:Namespace)-[:HAS_WORKLOAD]->(:Deployment|CronJob)
  (:Service)-[:TARGETS]->(:Deployment)
  (:Ingress)-[:ROUTES]->(:Service)
  (:Deployment)-[:MOUNTS]->(:ConfigMap|Secret)
  (:Deployment)-[:READS_ENV]->(:EnvVar)
  (:Deployment)-[:HAS_STATUS]->(:PodStatus)

Secret nodes carry key names only, never values.
"""
from __future__ import annotations

import argparse
import json
import sys

from neo4j import GraphDatabase

SCHEMA = [
    "CREATE CONSTRAINT cluster_name IF NOT EXISTS FOR (c:Cluster) REQUIRE c.name IS UNIQUE",
    "CREATE CONSTRAINT ns_cn IF NOT EXISTS FOR (n:Namespace) REQUIRE (n.cluster, n.name) IS UNIQUE",
    "CREATE CONSTRAINT deploy_cnn IF NOT EXISTS FOR (d:Deployment) REQUIRE (d.cluster, d.ns, d.name) IS UNIQUE",
    "CREATE CONSTRAINT svc_cnn IF NOT EXISTS FOR (s:Service) REQUIRE (s.cluster, s.ns, s.name) IS UNIQUE",
    "CREATE CONSTRAINT ing_cnn IF NOT EXISTS FOR (i:Ingress) REQUIRE (i.cluster, i.ns, i.name, i.host, i.path) IS UNIQUE",
    "CREATE CONSTRAINT cm_cnn IF NOT EXISTS FOR (c:ConfigMap) REQUIRE (c.cluster, c.ns, c.name) IS UNIQUE",
    "CREATE CONSTRAINT sec_cnn IF NOT EXISTS FOR (s:Secret) REQUIRE (s.cluster, s.ns, s.name) IS UNIQUE",
    "CREATE CONSTRAINT cj_cnn IF NOT EXISTS FOR (c:CronJob) REQUIRE (c.cluster, c.ns, c.name) IS UNIQUE",
    "CREATE CONSTRAINT env_name IF NOT EXISTS FOR (e:EnvVar) REQUIRE e.name IS UNIQUE",
    "CREATE CONSTRAINT pod_cnn IF NOT EXISTS FOR (p:PodStatus) REQUIRE (p.cluster, p.ns, p.name) IS UNIQUE",
    "CREATE CONSTRAINT img_repo IF NOT EXISTS FOR (i:DockerImage) REQUIRE i.repo IS UNIQUE",
]


def handle(session, rec: dict) -> None:
    kind = rec["kind"]
    if kind == "Cluster":
        session.run("MERGE (c:Cluster {name:$n}) SET c.env = $env",
                    n=rec["name"], env=rec.get("env"))
    elif kind == "Namespace":
        session.run("""
            MERGE (n:Namespace {cluster:$c, name:$name})
            WITH n
            MATCH (cl:Cluster {name:$c})
            MERGE (cl)-[:HAS_NS]->(n)
        """, c=rec["cluster"], name=rec["name"])
    elif kind == "Deployment":
        session.run("""
            MERGE (d:Deployment {cluster:$c, ns:$ns, name:$name})
            SET d.image = $image, d.replicas = $replicas,
                d.env_label = $env, d.labels = $labels, d.selector = $selector
            WITH d
            MATCH (ns:Namespace {cluster:$c, name:$ns})
            MERGE (ns)-[:HAS_WORKLOAD]->(d)
        """, c=rec["cluster"], ns=rec["ns"], name=rec["name"],
             image=rec.get("image"), replicas=rec.get("replicas"),
             env=rec.get("env"),
             labels=json.dumps(rec.get("labels") or {}),
             selector=json.dumps(rec.get("selector") or {}))

        for env_name in (rec.get("env_vars") or []):
            session.run("""
                MERGE (e:EnvVar {name:$en})
                WITH e
                MATCH (d:Deployment {cluster:$c, ns:$ns, name:$dn})
                MERGE (d)-[:READS_ENV]->(e)
            """, en=env_name, c=rec["cluster"], ns=rec["ns"], dn=rec["name"])

        for cm in (rec.get("configmap_mounts") or []):
            session.run("""
                MERGE (cm:ConfigMap {cluster:$c, ns:$ns, name:$n})
                WITH cm
                MATCH (d:Deployment {cluster:$c, ns:$ns, name:$dn})
                MERGE (d)-[:MOUNTS]->(cm)
            """, c=rec["cluster"], ns=rec["ns"], n=cm, dn=rec["name"])

        for sec in (rec.get("secret_mounts") or []):
            session.run("""
                MERGE (s:Secret {cluster:$c, ns:$ns, name:$n})
                WITH s
                MATCH (d:Deployment {cluster:$c, ns:$ns, name:$dn})
                MERGE (d)-[:MOUNTS]->(s)
            """, c=rec["cluster"], ns=rec["ns"], n=sec, dn=rec["name"])

        if rec.get("image"):
            repo = rec["image"].split(":")[0]
            session.run("""
                MERGE (i:DockerImage {repo:$r})
                SET i.last_tag = $tag
                WITH i
                MATCH (d:Deployment {cluster:$c, ns:$ns, name:$dn})
                MERGE (d)-[:RUNS_IMAGE]->(i)
            """, r=repo, tag=rec["image"].split(":")[-1] if ":" in rec["image"] else "latest",
                 c=rec["cluster"], ns=rec["ns"], dn=rec["name"])

    elif kind == "Service":
        # Neo4j rejects nested maps; serialize to JSON then decode in Cypher
        # via apoc.convert.fromJsonMap when a comparison is needed.
        sel = rec.get("selector") or {}
        session.run("""
            MERGE (s:Service {cluster:$c, ns:$ns, name:$name})
            SET s.ports = $ports, s.selector = $sel_json, s.type = $type
            WITH s
            MATCH (d:Deployment {cluster:$c, ns:$ns})
            WHERE size($sel_keys) > 0
              AND ALL(k IN $sel_keys
                      WHERE apoc.convert.fromJsonMap(d.selector)[k] = $sel_vals[k])
            MERGE (s)-[:TARGETS]->(d)
        """, c=rec["cluster"], ns=rec["ns"], name=rec["name"],
             ports=json.dumps(rec.get("ports") or []),
             sel_json=json.dumps(sel),
             sel_keys=list(sel.keys()),
             sel_vals=sel,
             type=rec.get("type"))
    elif kind == "Ingress":
        session.run("""
            MERGE (i:Ingress {cluster:$c, ns:$ns, name:$name, host:$host, path:$path})
            SET i.backend_svc = $svc, i.backend_port = $port
            WITH i
            OPTIONAL MATCH (s:Service {cluster:$c, ns:$ns, name:$svc})
            FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END |
                MERGE (i)-[:ROUTES]->(s))
        """, c=rec["cluster"], ns=rec["ns"], name=rec["name"],
             host=rec["host"] or "", path=rec["path"] or "/",
             svc=rec.get("backend_svc"), port=rec.get("backend_port"))
    elif kind == "ConfigMap":
        session.run("""
            MERGE (cm:ConfigMap {cluster:$c, ns:$ns, name:$name})
            SET cm.keys = $keys
        """, c=rec["cluster"], ns=rec["ns"], name=rec["name"],
             keys=rec.get("keys") or [])
    elif kind == "Secret":
        session.run("""
            MERGE (s:Secret {cluster:$c, ns:$ns, name:$name})
            SET s.keys = $keys, s.type = $type
        """, c=rec["cluster"], ns=rec["ns"], name=rec["name"],
             keys=rec.get("keys") or [], type=rec.get("type"))
    elif kind == "CronJob":
        session.run("""
            MERGE (cj:CronJob {cluster:$c, ns:$ns, name:$name})
            SET cj.schedule = $s, cj.image = $img, cj.command = $cmd, cj.args = $args
            WITH cj
            MATCH (ns:Namespace {cluster:$c, name:$ns})
            MERGE (ns)-[:HAS_WORKLOAD]->(cj)
        """, c=rec["cluster"], ns=rec["ns"], name=rec["name"],
             s=rec.get("schedule"), img=rec.get("image"),
             cmd=rec.get("command") or [], args=rec.get("args") or [])
    elif kind == "PodStatus":
        session.run("""
            MERGE (p:PodStatus {cluster:$c, ns:$ns, name:$name})
            SET p.phase = $phase, p.restarts = $r, p.image = $img,
                p.node = $n, p.env = $env
            WITH p
            OPTIONAL MATCH (d:Deployment {cluster:$c, ns:$ns, name:$owner})
            FOREACH (_ IN CASE WHEN d IS NULL THEN [] ELSE [1] END |
                MERGE (d)-[:HAS_STATUS]->(p))
        """, c=rec["cluster"], ns=rec["ns"], name=rec["name"],
             phase=rec.get("phase"), r=rec.get("restarts", 0),
             img=rec.get("image"), n=rec.get("node"),
             env=rec.get("env"), owner=(rec.get("owner") or ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--neo4j", default="bolt://127.0.0.1:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    args = ap.parse_args()

    drv = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))
    count = 0
    with drv.session() as s:
        for stmt in SCHEMA:
            s.run(stmt)
        for path in args.files:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        handle(s, json.loads(line))
                        count += 1
                    except Exception as exc:
                        print(f"  err: {exc}", file=sys.stderr)
    drv.close()
    print(f"ingested {count} k8s resources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
