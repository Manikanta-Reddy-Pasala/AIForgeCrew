#!/usr/bin/env python3
"""Backfill bge-m3 embeddings for memories rows that have NULL embedding.

Used after migrate-hindsight-to-aiforge.sh, or any bulk ingest path that
doesn't embed inline.

Run:
    python scripts/runtime/embed-backfill.py           # process up to 5000
    python scripts/runtime/embed-backfill.py --limit 500
    BATCH=64 python scripts/runtime/embed-backfill.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from aiforge_core.embed import embed_batch
from aiforge_core.runtime.config import AIFORGE_DSN


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--batch", type=int, default=int(os.environ.get("BATCH", "32")))
    args = ap.parse_args()

    with psycopg.connect(AIFORGE_DSN, autocommit=False) as c:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NULL")
            n_missing = cur.fetchone()[0]
        print(f"[backfill] rows missing embedding: {n_missing}")
        if n_missing == 0:
            return 0

        processed = 0
        while processed < args.limit:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, text FROM memories WHERE embedding IS NULL "
                    "ORDER BY id LIMIT %s",
                    (args.batch,),
                )
                rows = cur.fetchall()
            if not rows:
                break
            ids = [r[0] for r in rows]
            texts = [r[1][:8000] for r in rows]
            t0 = time.time()
            vectors = embed_batch(texts)
            with c.cursor() as cur:
                for rid, v in zip(ids, vectors, strict=True):
                    cur.execute(
                        "UPDATE memories SET embedding=%s WHERE id=%s",
                        (v, rid),
                    )
            c.commit()
            dt = time.time() - t0
            processed += len(rows)
            print(f"[backfill] +{len(rows)} rows in {dt:.2f}s (total {processed})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
