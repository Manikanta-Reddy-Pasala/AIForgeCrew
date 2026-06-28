"""Cypher writer for Chunk_v2 nodes + CHUNKED_AS edges.

Writes are batched (``UNWIND $rows`` in slices of ``_BATCH``) — one
Cypher round trip per 500 chunks instead of one per chunk.
"""
from __future__ import annotations

from aiforge_memory.features.chunk.embed import WalkedChunk

_BATCH = 500

_UPSERT_CHUNKS = """
UNWIND $rows AS r
MERGE (c:Chunk_v2 {id: r.id})
SET c.repo        = r.repo,
    c.file_path   = r.file_path,
    c.text        = r.text,
    c.token_count = r.token_count,
    c.line_start  = r.line_start,
    c.line_end    = r.line_end,
    c.schema_version = 'codemem-v1'
WITH c, r
MATCH (f:File_v2 {repo: $repo, path: r.file_path})
MERGE (f)-[:CHUNKED_AS]->(c)
"""

_PRUNE_FILE_CHUNKS = """
UNWIND $rows AS r
MATCH (f:File_v2 {repo: $repo, path: r.path})-[:CHUNKED_AS]->(c:Chunk_v2)
WHERE NOT c.id IN r.chunk_ids
DETACH DELETE c
"""


def _batched(rows: list, n: int = _BATCH):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def upsert_chunks(driver, *, repo: str, chunks: list[WalkedChunk]) -> dict:
    counts = {"chunks": 0, "pruned": 0}
    by_path: dict[str, list[str]] = {}
    for c in chunks:
        by_path.setdefault(c.file_path, []).append(c.id)

    prune_rows = [
        {"path": path, "chunk_ids": ids} for path, ids in by_path.items()
    ]
    chunk_rows = [
        {"id": c.id, "repo": c.repo, "file_path": c.file_path,
         "text": c.text,
         "token_count": c.token_count,
         "line_start": c.line_start, "line_end": c.line_end}
        for c in chunks
    ]

    with driver.session() as sess:
        # prune stale chunks per-file
        for batch in _batched(prune_rows):
            r = sess.run(_PRUNE_FILE_CHUNKS, repo=repo, rows=batch).consume()
            counts["pruned"] += r.counters.nodes_deleted

        for batch in _batched(chunk_rows):
            sess.run(_UPSERT_CHUNKS, repo=repo, rows=batch).consume()
            counts["chunks"] += len(batch)
    return counts
