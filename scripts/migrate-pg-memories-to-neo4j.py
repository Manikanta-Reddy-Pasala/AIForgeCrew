#!/usr/bin/env python3
"""One-shot migration: Postgres ``memories`` rows → Neo4j ``(:Memory)`` nodes.

Part of the Option-A consolidation (2026-04-24): retire Postgres as the
agent memory store, keep it only for tickets/events/checkpoints.

Run on the NUC (direct Postgres + direct Neo4j):

    ~/aiforge-venv/bin/python scripts/migrate-pg-memories-to-neo4j.py \\
        --batch 500 --limit 0   # limit=0 means no cap

Idempotent: keys on ``fact_id = f"pgmem-{id}"`` so re-runs MERGE, don't duplicate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import psycopg
from neo4j import GraphDatabase

PG_DSN = os.environ.get(
    "AIFORGE_DSN",
    "postgresql://aiforge:aiforgepass@127.0.0.1:5432/aiforge",
)
NEO4J_URI = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
EMBED_DIM = int(os.environ.get("AIFORGE_EMBED_DIM", "1024"))


SCHEMA = [
    "CREATE CONSTRAINT memory_fact_id IF NOT EXISTS FOR (m:Memory) "
    "REQUIRE m.fact_id IS UNIQUE",
    "CREATE INDEX memory_tier_wing IF NOT EXISTS FOR (m:Memory) "
    "ON (m.tier, m.wing)",
    "CREATE FULLTEXT INDEX memory_text_ft IF NOT EXISTS FOR (m:Memory) "
    "ON EACH [m.title, m.text]",
    f"CREATE VECTOR INDEX memory_embedding_vec IF NOT EXISTS "
    f"FOR (m:Memory) ON (m.embedding) OPTIONS {{indexConfig: {{"
    f"  `vector.dimensions`: {EMBED_DIM}, "
    f"  `vector.similarity_function`: 'cosine' "
    f"}}}}",
]


UPSERT = """
UNWIND $rows AS r
MERGE (m:Memory {fact_id: r.fact_id})
SET m.tier = r.tier,
    m.wing = r.wing,
    m.kind = r.kind,
    m.title = r.title,
    m.text = r.text,
    m.source = r.source,
    m.parent_id = r.parent_id,
    m.metadata = r.metadata,
    m.embedding = r.embedding,
    m.created_at = r.created_at,
    m.expires_at = r.expires_at,
    m.pg_id = r.pg_id
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--limit", type=int, default=0,
                    help="0 = no limit; small int for smoke test")
    ap.add_argument("--tier", help="Migrate only this tier (t1/t2/t3/t4)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # Pull from Postgres
    pg = psycopg.connect(PG_DSN, connect_timeout=10)
    where = ""
    params: tuple = ()
    if args.tier:
        where = "WHERE tier = %s"
        params = (args.tier,)
    limit_clause = f"LIMIT {args.limit}" if args.limit > 0 else ""

    with pg.cursor() as cur:
        cur.execute(
            f"SELECT id, tier, wing, kind, source, title, text, embedding, "
            f"       metadata, parent_id, created_at, expires_at "
            f"FROM memories {where} "
            f"ORDER BY id {limit_clause}",
            params,
        )
        rows = cur.fetchall()
    print(f"pulled {len(rows)} rows from postgres")

    if args.dry_run:
        print("DRY RUN — not writing")
        return 0

    # Prep Neo4j
    drv = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    with drv.session() as s:
        for q in SCHEMA:
            try:
                s.run(q)
            except Exception as exc:
                print(f"schema init warning: {exc}", file=sys.stderr)

    # Batch upsert. psycopg returns the pgvector ``embedding`` as string;
    # parse defensively so both text and list paths work.
    def _emb(v):
        if v is None:
            return None
        if isinstance(v, list):
            return list(v)
        if isinstance(v, str) and v.startswith("["):
            try:
                return [float(x) for x in v.strip("[] ").split(",") if x.strip()]
            except Exception:
                return None
        try:
            return list(v)
        except Exception:
            return None

    def _to_row(r):
        (pg_id, tier, wing, kind, source, title, text, embedding,
         metadata, parent_id, created_at, expires_at) = r
        return {
            "fact_id": f"pgmem-{pg_id}",
            "pg_id": int(pg_id),
            "tier": tier or "",
            "wing": wing or "",
            "kind": kind or "",
            "source": source or "",
            "title": title or "",
            "text": text or "",
            "embedding": _emb(embedding),
            "metadata": json.dumps(metadata or {}),
            "parent_id": (parent_id or ""),
            "created_at": created_at.isoformat() if created_at else None,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }

    t0 = time.time()
    total = 0
    with drv.session() as s:
        buf = []
        for r in rows:
            buf.append(_to_row(r))
            if len(buf) >= args.batch:
                s.run(UPSERT, rows=buf)
                total += len(buf)
                dt = time.time() - t0
                print(f"  {total}/{len(rows)} in {dt:.1f}s "
                      f"({total/dt:.0f}/s)")
                buf = []
        if buf:
            s.run(UPSERT, rows=buf)
            total += len(buf)
    print(f"migrated {total} in {time.time()-t0:.1f}s")

    with drv.session() as s:
        n = s.run("MATCH (m:Memory) RETURN count(m) AS n").single()["n"]
        print(f"neo4j now holds {n} :Memory nodes")

    pg.close()
    drv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
