"""Kubernetes read-only tools. Uses `kubectl` subprocess for portability.

Writes (restart, scale, delete) require explicit `confirm:true`.
Kubeconfig per env read from env vars QA_KUBECONFIG / PROD_KUBECONFIG.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from ..cypher_lib import session


def _kc(env: str) -> str:
    var = "QA_KUBECONFIG" if env == "qa" else "PROD_KUBECONFIG"
    path = os.environ.get(var)
    if not path:
        raise RuntimeError(f"{var} not set")
    return os.path.expanduser(path)


def _kubectl(args: list[str], env: str, timeout: int = 20) -> str:
    cmd = ["kubectl", "--kubeconfig", _kc(env), "--insecure-skip-tls-verify=true", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return r.stdout + r.stderr
    return r.stdout


def kube_status(args: dict) -> dict:
    deploy = args["deployment"]
    env = args.get("env", "qa")
    ns = args.get("ns")
    if not ns:
        with session() as s:
            rec = s.run(
                "MATCH (d:Deployment {name:$n, env_label:$env}) RETURN d.ns AS ns",
                n=deploy, env=env).single()
            ns = rec["ns"] if rec else "default"
    out = _kubectl(["-n", ns, "get", "deploy", deploy, "-o", "wide"], env)
    pods = _kubectl(["-n", ns, "get", "pods", "-l", f"app={deploy}", "-o", "wide"], env)
    return {"deployment": deploy, "env": env, "ns": ns, "deploy": out, "pods": pods}


def kube_describe(args: dict) -> dict:
    resource = args["resource"]
    name = args["name"]
    ns = args.get("ns", "default")
    env = args.get("env", "qa")
    out = _kubectl(["-n", ns, "describe", resource, name], env, timeout=30)
    return {"describe": out[:20000]}


def kube_image_tag(args: dict) -> dict:
    deploy = args["deployment"]
    env = args.get("env", "qa")
    ns = args.get("ns", "default")
    out = _kubectl(
        ["-n", ns, "get", "deploy", deploy, "-o",
         "jsonpath={.spec.template.spec.containers[0].image}"],
        env,
    )
    return {"deployment": deploy, "env": env, "image": out.strip()}


def kube_config(args: dict) -> dict:
    """Return env vars + configmap keys + secret keys attached to a deployment."""
    deploy = args["deployment"]
    env = args.get("env", "qa")
    cy = """
    MATCH (d:Deployment {name:$n, env_label:$env})
    OPTIONAL MATCH (d)-[:READS_ENV]->(e:EnvVar)
    OPTIONAL MATCH (d)-[:MOUNTS]->(cm:ConfigMap)
    OPTIONAL MATCH (d)-[:MOUNTS]->(sec:Secret)
    RETURN d.image AS image, d.replicas AS replicas,
           collect(DISTINCT e.name) AS env_vars,
           collect(DISTINCT {name: cm.name, keys: cm.keys}) AS configmaps,
           collect(DISTINCT {name: sec.name, keys: sec.keys}) AS secrets
    """
    with session() as s:
        rec = s.run(cy, n=deploy, env=env).single()
        return dict(rec) if rec else {"error": "not found"}


def kube_port_forward_cmd(args: dict) -> dict:
    svc = args["service"]
    env = args.get("env", "qa")
    ns = args.get("ns", "default")
    local = int(args.get("local_port", 8080))
    remote = int(args.get("remote_port", 8080))
    cfg = os.environ.get("QA_KUBECONFIG" if env == "qa" else "PROD_KUBECONFIG", "")
    cmd = (f"kubectl --kubeconfig {cfg} --insecure-skip-tls-verify "
           f"port-forward svc/{svc} {local}:{remote} -n {ns}")
    return {"cmd": cmd, "note": "User must run this themselves; tool never executes port-forward."}


def kube_rollout_restart(args: dict) -> dict:
    if not args.get("confirm"):
        return {"error": "Write op requires confirm:true."}
    deploy = args["deployment"]
    env = args.get("env", "qa")
    ns = args.get("ns", "default")
    out = _kubectl(["-n", ns, "rollout", "restart", "deployment", deploy], env)
    return {"result": out}


TOOLS = [
    {
        "name": "kube_status",
        "description": "Deployment + pod summary for a service in an env (qa/prod).",
        "input_schema": {
            "type": "object",
            "properties": {
                "deployment": {"type": "string"},
                "env": {"type": "string", "enum": ["qa", "prod"], "default": "qa"},
                "ns": {"type": "string"},
            },
            "required": ["deployment"],
        },
    },
    {
        "name": "kube_describe",
        "description": "kubectl describe <resource> <name> -n <ns>.",
        "input_schema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string"},
                "name": {"type": "string"},
                "ns": {"type": "string"},
                "env": {"type": "string", "default": "qa"},
            },
            "required": ["resource", "name"],
        },
    },
    {
        "name": "kube_image_tag",
        "description": "Currently deployed container image tag.",
        "input_schema": {
            "type": "object",
            "properties": {
                "deployment": {"type": "string"},
                "env": {"type": "string", "default": "qa"},
                "ns": {"type": "string"},
            },
            "required": ["deployment"],
        },
    },
    {
        "name": "kube_config",
        "description": "Env vars + configmap + secret keys mounted on a Deployment (graph cache, no cluster hit).",
        "input_schema": {
            "type": "object",
            "properties": {
                "deployment": {"type": "string"},
                "env": {"type": "string", "default": "qa"},
            },
            "required": ["deployment"],
        },
    },
    {
        "name": "kube_port_forward_cmd",
        "description": "Return a kubectl port-forward command string for user to run manually (tool does not execute).",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "env": {"type": "string", "default": "qa"},
                "ns": {"type": "string", "default": "default"},
                "local_port": {"type": "integer", "default": 8080},
                "remote_port": {"type": "integer", "default": 8080},
            },
            "required": ["service"],
        },
    },
    {
        "name": "kube_rollout_restart",
        "description": "Restart a deployment. Requires confirm:true; writes to cluster.",
        "input_schema": {
            "type": "object",
            "properties": {
                "deployment": {"type": "string"},
                "env": {"type": "string", "default": "qa"},
                "ns": {"type": "string", "default": "default"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["deployment", "confirm"],
        },
    },
]

HANDLERS = {
    "kube_status": kube_status,
    "kube_describe": kube_describe,
    "kube_image_tag": kube_image_tag,
    "kube_config": kube_config,
    "kube_port_forward_cmd": kube_port_forward_cmd,
    "kube_rollout_restart": kube_rollout_restart,
}
