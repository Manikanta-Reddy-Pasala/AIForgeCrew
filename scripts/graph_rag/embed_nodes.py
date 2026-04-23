#!/usr/bin/env python3
"""Embed Class + Method nodes in the graph and attach vectors so Neo4j
can do hybrid semantic + structural traversal (e.g. "find the 5 methods
most related to 'pagination' and then walk 2 hops of CALLS out").

Requires LM Studio exposing an OpenAI-compatible /v1/embeddings endpoint.

Usage:
    # Laptop side, with an ssh tunnel to Mac Studio's LM Studio:
    ssh -f -N -L 1235:localhost:1234 manikanta@192.168.70.185
    python embed_nodes.py --lm http://127.0.0.1:1235/v1 \\
        --model text-embedding-nomic-embed-text-v1.5
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx
from neo4j import GraphDatabase


def embed_batch(base: str, model: str, texts: list[str],
                api_key: str = "lm-studio",
                dim: int | None = None, timeout: float = 60.0
                ) -> list[list[float]]:
    resp = httpx.post(
        f"{base}/embeddings",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={"model": model, "input": texts},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    out = [d["embedding"] for d in data]
    if dim is not None:
        assert all(len(v) == dim for v in out), f"unexpected dim; got {len(out[0])}"
    return out


def method_text(record: dict) -> str:
    parts = [
        f"{record.get('class','?')}.{record.get('name','?')}{record.get('sig','()')}",
        f"returns {record.get('return_type','') or 'void'}",
    ]
    ann = record.get("annotations") or []
    if ann:
        parts.append("annotations: " + " ".join("@" + a for a in ann))
    jd = record.get("javadoc") or ""
    if jd:
        parts.append(jd.replace("/**", "").replace("*/", "").replace("\n * ", "\n").strip())
    body = record.get("body_snippet") or ""
    parts.append(body[:1800])
    return "\n".join(p for p in parts if p).strip()[:4000]


def class_text(record: dict) -> str:
    parts = [
        f"{record.get('simple','?')} "
        f"({record.get('layer','other')} {record.get('kind','class')})",
        f"package {record.get('package','')}",
    ]
    jd = record.get("javadoc") or ""
    if jd:
        parts.append(jd.replace("/**", "").replace("*/", "").replace("\n * ", "\n").strip())
    ann = record.get("annotations") or []
    if ann:
        parts.append("annotations: " + " ".join("@" + a for a in ann))
    imports = record.get("imports") or []
    parts.append("imports: " + ", ".join(i.split(".")[-1] for i in imports[:20]))
    methods = record.get("method_names") or []
    if methods:
        parts.append("methods: " + ", ".join(methods[:20]))
    return "\n".join(p for p in parts if p).strip()[:4000]


def ensure_vector_index(session, label: str, prop: str, dim: int) -> None:
    name = f"{label.lower()}_{prop}_vec"
    session.run(
        f"CREATE VECTOR INDEX {name} IF NOT EXISTS "
        f"FOR (n:{label}) ON (n.{prop}) "
        f"OPTIONS {{indexConfig: {{"
        f"  `vector.dimensions`: {dim}, "
        f"  `vector.similarity_function`: 'cosine' "
        f"}}}}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--neo4j", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="password")
    ap.add_argument("--lm", default="http://127.0.0.1:1235/v1",
                    help="LM Studio base URL (OpenAI-compatible)")
    ap.add_argument("--model", default="text-embedding-nomic-embed-text-v1.5")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--dim", type=int, default=768)
    ap.add_argument("--skip-classes", action="store_true")
    ap.add_argument("--skip-methods", action="store_true")
    ap.add_argument("--extras", action="store_true",
                    help="Also embed Endpoint, MongoCollection, NatsSubject, KafkaTopic, Memory.")
    ap.add_argument("--only-new", action="store_true",
                    help="Skip rows that already have an embedding. Default on.")
    args = ap.parse_args()

    drv = GraphDatabase.driver(args.neo4j, auth=(args.user, args.password))
    with drv.session() as s:
        ensure_vector_index(s, "Method", "embedding", args.dim)
        ensure_vector_index(s, "Class", "embedding", args.dim)

        if not args.skip_methods:
            rows = [dict(r) for r in s.run(
                "MATCH (c:Class)-[:CONTAINS]->(m:Method) "
                "WHERE m.embedding IS NULL "
                "RETURN m.fqn AS fqn, m.name AS name, m.sig AS sig, "
                "m.return_type AS return_type, m.javadoc AS javadoc, "
                "m.body_snippet AS body_snippet, m.annotations AS annotations, "
                "c.simple AS class"
            )]
            print(f"methods to embed: {len(rows)}")
            _embed_and_store(s, rows, method_text, "Method", args)

        if not args.skip_classes:
            rows = [dict(r) for r in s.run(
                "MATCH (c:Class) WHERE c.embedding IS NULL "
                "OPTIONAL MATCH (c)-[:CONTAINS]->(m:Method) "
                "WITH c, collect(m.name)[0..25] AS method_names "
                "RETURN c.fqn AS fqn, c.simple AS simple, c.kind AS kind, "
                "c.layer AS layer, c.package AS package, "
                "c.javadoc AS javadoc, c.annotations AS annotations, "
                "c.imports AS imports, method_names"
            )]
            print(f"classes to embed: {len(rows)}")
            _embed_and_store(s, rows, class_text, "Class", args)

        if args.extras:
            _embed_label(s, "Endpoint",
                "MATCH (e:Endpoint) WHERE e.embedding IS NULL "
                "OPTIONAL MATCH (m:Method)-[:EXPOSES]->(e) "
                "RETURN e.path AS key, e.http AS http, e.path AS path, "
                "collect(DISTINCT m.fqn)[0..5] AS handlers, "
                "collect(DISTINCT m.javadoc)[0..3] AS docs",
                lambda r: f"HTTP {r.get('http') or ''} {r['path']}\n"
                          f"handlers: {', '.join(r.get('handlers') or [])}\n"
                          f"{' '.join((r.get('docs') or []))[:800]}",
                "key_field=path", args)

            _embed_label(s, "MongoCollection",
                "MATCH (c:MongoCollection) WHERE c.embedding IS NULL "
                "OPTIONAL MATCH (m:Method)-[r:READS|WRITES|DELETES]->(c) "
                "WITH c, type(r) AS op, m.fqn AS fqn "
                "RETURN c.name AS key, c.name AS name, "
                "collect(DISTINCT op) AS ops, "
                "collect(DISTINCT fqn)[0..10] AS methods",
                lambda r: f"Mongo collection {r['name']}\n"
                          f"ops: {', '.join(r.get('ops') or [])}\n"
                          f"accessed by: {', '.join((r.get('methods') or [])[:10])}",
                "key_field=name", args)

            _embed_label(s, "NatsSubject",
                "MATCH (s:NatsSubject) WHERE s.embedding IS NULL "
                "OPTIONAL MATCH (m:Method)-[r:PUBLISH|SUBSCRIBE]->(s) "
                "WITH s, type(r) AS role, m.fqn AS fqn "
                "RETURN s.subject AS key, s.subject AS subject, "
                "collect(DISTINCT role) AS roles, "
                "collect(DISTINCT fqn)[0..10] AS methods",
                lambda r: f"NATS subject {r['subject']}\n"
                          f"roles: {', '.join(r.get('roles') or [])}\n"
                          f"methods: {', '.join((r.get('methods') or [])[:10])}",
                "key_field=subject", args)

            _embed_label(s, "KafkaTopic",
                "MATCH (t:KafkaTopic) WHERE t.embedding IS NULL "
                "OPTIONAL MATCH (m:Method)-[r:PRODUCES|CONSUMES]->(t) "
                "WITH t, type(r) AS role, m.fqn AS fqn "
                "RETURN t.name AS key, t.name AS name, "
                "collect(DISTINCT role) AS roles, "
                "collect(DISTINCT fqn)[0..10] AS methods",
                lambda r: f"Kafka topic {r['name']}\n"
                          f"roles: {', '.join(r.get('roles') or [])}\n"
                          f"methods: {', '.join((r.get('methods') or [])[:10])}",
                "key_field=name", args)

            _embed_label(s, "Memory",
                "MATCH (m:Memory) WHERE m.embedding IS NULL "
                "RETURN m.path AS key, m.title AS title, m.type AS type, "
                "m.description AS description, m.body AS body",
                lambda r: f"{r.get('title') or ''} ({r.get('type') or ''})\n"
                          f"{r.get('description') or ''}\n"
                          f"{(r.get('body') or '')[:3000]}",
                "key_field=path", args)

    drv.close()
    return 0


def _embed_label(session, label: str, cypher_fetch: str, text_fn,
                 key_spec: str, args) -> None:
    """Embed any label where we can identify rows by a single key field."""
    ensure_vector_index(session, label, "embedding", args.dim)
    rows = [dict(r) for r in session.run(cypher_fetch)]
    print(f"{label.lower()}s to embed: {len(rows)}")
    if not rows:
        return
    key_field = key_spec.split("=", 1)[1]
    import time as _t
    t0 = _t.time()
    for i in range(0, len(rows), args.batch):
        batch = rows[i:i + args.batch]
        texts = [text_fn(r) for r in batch]
        try:
            vecs = embed_batch(args.lm, args.model, texts, dim=args.dim)
        except Exception as exc:
            print(f"  {label} batch {i} failed: {exc}", file=sys.stderr)
            continue
        session.run(
            f"UNWIND $rows AS r "
            f"MATCH (n:{label} {{{key_field}: r.key}}) "
            f"SET n.embedding = r.vec",
            rows=[{"key": r["key"], "vec": v} for r, v in zip(batch, vecs)],
        )
    print(f"  {label} done: {len(rows)} in {_t.time()-t0:.1f}s")


def _embed_and_store(session, rows: list[dict], text_fn, label: str, args) -> None:
    if not rows:
        return
    t0 = time.time()
    for i in range(0, len(rows), args.batch):
        batch = rows[i:i + args.batch]
        texts = [text_fn(r) for r in batch]
        try:
            vecs = embed_batch(args.lm, args.model, texts, dim=args.dim)
        except Exception as exc:
            print(f"  batch {i} failed: {exc}", file=sys.stderr)
            continue
        session.run(
            f"UNWIND $rows AS r "
            f"MATCH (n:{label} {{fqn: r.fqn}}) "
            f"SET n.embedding = r.vec",
            rows=[{"fqn": r["fqn"], "vec": v}
                  for r, v in zip(batch, vecs)],
        )
        if (i // args.batch) % 20 == 0:
            dt = time.time() - t0
            done = min(i + args.batch, len(rows))
            rate = done / dt if dt else 0
            print(f"  {done}/{len(rows)}  {rate:.1f}/s")
    print(f"done {label}: {len(rows)} in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main())
