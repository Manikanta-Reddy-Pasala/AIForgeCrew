#!/usr/bin/env python3
"""Snapshot Kubernetes cluster state to JSONL. READ-ONLY.

Emits one JSON object per k8s resource. Consumed by ingest_k8s.py.

Secrets: names + keys only, never values.
"""
from __future__ import annotations

import argparse
import json
import sys

from kubernetes import client, config


def snapshot(ctx_name: str, kubeconfig: str, env: str, namespaces: list[str]):
    cfg = client.Configuration()
    config.load_kube_config(config_file=kubeconfig, context=ctx_name,
                            client_configuration=cfg)
    cfg.verify_ssl = False
    api_client = client.ApiClient(configuration=cfg)

    core = client.CoreV1Api(api_client)
    apps = client.AppsV1Api(api_client)
    net = client.NetworkingV1Api(api_client)
    batch = client.BatchV1Api(api_client)

    yield {"kind": "Cluster", "name": ctx_name, "env": env}

    for ns in namespaces:
        yield {"kind": "Namespace", "cluster": ctx_name, "name": ns}

        for d in apps.list_namespaced_deployment(ns).items:
            containers = d.spec.template.spec.containers or []
            c0 = containers[0] if containers else None
            yield {
                "kind": "Deployment",
                "cluster": ctx_name, "env": env, "ns": ns,
                "name": d.metadata.name,
                "image": c0.image if c0 else None,
                "replicas": d.spec.replicas,
                "labels": d.metadata.labels or {},
                "selector": (d.spec.selector.match_labels or {}) if d.spec.selector else {},
                "env_vars": sorted({e.name for c in containers for e in (c.env or [])}),
                "volumes": [v.name for v in (d.spec.template.spec.volumes or [])],
                "configmap_mounts": [
                    v.config_map.name
                    for v in (d.spec.template.spec.volumes or [])
                    if v.config_map
                ],
                "secret_mounts": [
                    v.secret.secret_name
                    for v in (d.spec.template.spec.volumes or [])
                    if v.secret
                ],
            }

        for svc in core.list_namespaced_service(ns).items:
            yield {
                "kind": "Service",
                "cluster": ctx_name, "ns": ns,
                "name": svc.metadata.name,
                "selector": svc.spec.selector or {},
                "ports": [
                    {"port": p.port, "target_port": p.target_port, "proto": p.protocol}
                    for p in (svc.spec.ports or [])
                ],
                "type": svc.spec.type,
            }

        for ing in net.list_namespaced_ingress(ns).items:
            for r in (ing.spec.rules or []):
                for p in (r.http.paths if r.http else []) or []:
                    yield {
                        "kind": "Ingress",
                        "cluster": ctx_name, "ns": ns,
                        "name": ing.metadata.name,
                        "host": r.host,
                        "path": p.path,
                        "backend_svc": p.backend.service.name if p.backend.service else None,
                        "backend_port": (
                            p.backend.service.port.number
                            if p.backend.service and p.backend.service.port else None
                        ),
                    }

        for cm in core.list_namespaced_config_map(ns).items:
            yield {
                "kind": "ConfigMap",
                "cluster": ctx_name, "ns": ns,
                "name": cm.metadata.name,
                "keys": list((cm.data or {}).keys()),
            }

        for sec in core.list_namespaced_secret(ns).items:
            # Values never leave the cluster.
            yield {
                "kind": "Secret",
                "cluster": ctx_name, "ns": ns,
                "name": sec.metadata.name,
                "type": sec.type,
                "keys": list((sec.data or {}).keys()),
            }

        for cj in batch.list_namespaced_cron_job(ns).items:
            containers = cj.spec.job_template.spec.template.spec.containers or []
            c0 = containers[0] if containers else None
            yield {
                "kind": "CronJob",
                "cluster": ctx_name, "ns": ns,
                "name": cj.metadata.name,
                "schedule": cj.spec.schedule,
                "image": c0.image if c0 else None,
                "command": (c0.command or []) if c0 else [],
                "args": (c0.args or []) if c0 else [],
            }

        for pod in core.list_namespaced_pod(ns).items:
            cs = pod.status.container_statuses or []
            restarts = sum(int(c.restart_count or 0) for c in cs)
            owner = None
            if pod.metadata.owner_references:
                owner = pod.metadata.owner_references[0].name
            yield {
                "kind": "PodStatus",
                "cluster": ctx_name, "env": env, "ns": ns,
                "name": pod.metadata.name,
                "owner": owner,
                "phase": pod.status.phase,
                "restarts": restarts,
                "image": (pod.spec.containers[0].image if pod.spec.containers else None),
                "node": pod.spec.node_name,
            }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--kubeconfig", required=True)
    ap.add_argument("--context", required=True)
    ap.add_argument("--namespaces", nargs="+",
                    default=["default", "pos", "mongodb"])
    args = ap.parse_args()

    for node in snapshot(args.context, args.kubeconfig, args.env, args.namespaces):
        print(json.dumps(node))
    return 0


if __name__ == "__main__":
    sys.exit(main())
