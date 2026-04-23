#!/usr/bin/env python3
"""Build a 'ticket brief' context pack: everything Qwen needs to work a
ticket in one JSON blob. Uses: ticket_client + semantic search + impact +
repo_meta + k8s status + docs.

CLI:
    python ticket_brief.py ONE-57
    python ticket_brief.py --text "Add pagination to listing API" --no-ticket
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from neo4j import GraphDatabase

import ticket_client
import impact as impact_mod

EMBED_URL = os.environ.get("EMBED_URL", "http://127.0.0.1:1235/v1")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")
RERANK_URL = os.environ.get("RERANK_URL", "")


def embed(text: str) -> list[float]:
    try:
        r = httpx.post(
            f"{EMBED_URL}/embeddings",
            json={"model": EMBED_MODEL, "input": text},
            headers={"Authorization": "Bearer lm-studio"},
            timeout=30,
        )
        r.raise_for_status()
        j = r.json()
        if isinstance(j, dict) and "data" in j:
            return j["data"][0]["embedding"]
    except Exception:
        pass
    r = httpx.post(f"{EMBED_URL}/embed", json={"inputs": text}, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data[0] if isinstance(data, list) and data and isinstance(data[0], list) else data


def vector_search(session, vec: list[float], label: str, prop="embedding", k=10):
    q = (
        f"CALL db.index.vector.queryNodes('{label.lower()}_{prop}_vec', $k, $v) "
        f"YIELD node, score RETURN node, score"
    )
    try:
        return [dict(n=dict(r["node"]), score=r["score"]) for r in session.run(q, k=k, v=vec)]
    except Exception:
        return []


def fulltext(session, query: str, label="Method", k=20):
    idx = {
        "Method": "method_text",
        "Memory": "memory_text",
    }.get(label, "memory_text")
    q = (
        f"CALL db.index.fulltext.queryNodes('{idx}', $q) "
        f"YIELD node, score RETURN node, score LIMIT {k}"
    )
    try:
        return [dict(n=dict(r["node"]), score=r["score"]) for r in session.run(q, q=query)]
    except Exception:
        return []


def rerank(query: str, candidates: list[dict], key="text") -> list[dict]:
    if not candidates or not RERANK_URL:
        return candidates
    try:
        r = httpx.post(
            f"{RERANK_URL}/rerank",
            json={"query": query, "texts": [c.get(key, "") for c in candidates]},
            timeout=30,
        )
        r.raise_for_status()
        scores = r.json()
        return [c | {"rerank": s["score"]}
                for c, s in sorted(
                    zip(candidates, scores), key=lambda x: x[1]["score"], reverse=True)]
    except Exception:
        return candidates


def build(ticket_id: str | None, text: str | None, provider: str, neo4j: str):
    if ticket_id and not text:
        try:
            t = ticket_client.fetch(ticket_id, provider)
            text = f"{t.get('title','')}\n\n{t.get('body','')}"
        except Exception as exc:
            t = {"id": ticket_id, "error": str(exc)}
            text = ticket_id
    else:
        t = {"id": ticket_id or "ad-hoc", "body": text}

    drv = GraphDatabase.driver(neo4j, auth=("neo4j", "password"))
    brief = {"ticket": t, "query": text}
    with drv.session() as s:
        vec = embed(text or "")
        method_hits = vector_search(s, vec, "Method")
        memory_hits = vector_search(s, vec, "Memory")
        endpoint_hits = vector_search(s, vec, "Endpoint")

        brief["candidate_services"] = list({h["n"].get("repo") or h["n"].get("package","?").split(".")[0]
                                            for h in method_hits[:8]})
        brief["symbols"] = [
            {"fqn": h["n"].get("fqn"), "sig": h["n"].get("sig"),
             "file": h["n"].get("file"), "score": h["score"]}
            for h in method_hits[:10]
        ]
        brief["endpoints"] = [
            {"path": h["n"].get("path"), "http": h["n"].get("http"), "score": h["score"]}
            for h in endpoint_hits[:5]
        ]
        brief["related_memories"] = [
            {"path": h["n"].get("path"), "title": h["n"].get("title"),
             "type": h["n"].get("type"), "score": h["score"]}
            for h in memory_hits[:8]
        ]

        impacts = []
        for sym in brief["symbols"][:3]:
            if sym.get("fqn"):
                try:
                    imp = impact_mod.compute(drv, sym["fqn"])
                    impacts.append({"target": sym["fqn"], "impact": imp})
                except Exception as exc:
                    impacts.append({"target": sym["fqn"], "error": str(exc)})
        brief["impact"] = impacts

        if brief["candidate_services"]:
            r = s.run(
                "MATCH (r:Repo) WHERE r.name IN $names "
                "RETURN r.name AS name, r.lang AS lang, r.build_install AS install, "
                "r.build_test AS test, r.build_package AS package, "
                "r.depends_on AS depends_on, r.image_prefix AS image",
                names=brief["candidate_services"])
            brief["build_info"] = [dict(x) for x in r]

            r = s.run(
                "MATCH (r:Repo)-[:IS_SERVICE]->(d:Deployment)-[:HAS_STATUS]->(p:PodStatus) "
                "WHERE r.name IN $names "
                "RETURN r.name AS repo, d.env_label AS env, d.ns AS ns, d.name AS deploy, "
                "p.phase AS phase, p.restarts AS restarts, p.image AS image",
                names=brief["candidate_services"])
            brief["kube"] = [dict(x) for x in r]
    drv.close()
    return brief


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticket_id", nargs="?")
    ap.add_argument("--text")
    ap.add_argument("--provider", default="default")
    ap.add_argument("--neo4j", default="bolt://127.0.0.1:7687")
    ap.add_argument("--no-ticket", action="store_true")
    args = ap.parse_args()

    tid = None if args.no_ticket else args.ticket_id
    brief = build(tid, args.text, args.provider, args.neo4j)
    print(json.dumps(brief, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
