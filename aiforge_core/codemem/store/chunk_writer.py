"""Cypher writer for Chunk_v2 nodes + CHUNKED_AS edges."""
from __future__ import annotations

from aiforge_core.codemem.ingest.embed import WalkedChunk


_UPSERT_CHUNK = """
MERGE (c:Chunk_v2 {id: $id})
SET c.repo        = $repo,
    c.file_path   = $file_path,
    c.text        = $text,
    c.embed_vec   = $embed_vec,
    c.token_count = $token_count,
    c.line_start  = $line_start,
    c.line_end    = $line_end,
    c.schema_version = 'codemem-v1'
WITH c
MATCH (f:File_v2 {repo: $repo, path: $file_path})
MERGE (f)-[:CHUNKED_AS]->(c)
"""

_PRUNE_FILE_CHUNKS = """
MATCH (f:File_v2 {repo: $repo, path: $path})-[r:CHUNKED_AS]->(c:Chunk_v2)
WHERE NOT c.id IN $chunk_ids
DETACH DELETE c
"""


def upsert_chunks(driver, *, repo: str, chunks: list[WalkedChunk]) -> dict:
    counts = {"chunks": 0, "pruned": 0}
    by_path: dict[str, list[str]] = {}
    for c in chunks:
        by_path.setdefault(c.file_path, []).append(c.id)

    with driver.session() as sess:
        # prune stale chunks per-file
        for path, ids in by_path.items():
            r = sess.run(
                _PRUNE_FILE_CHUNKS, repo=repo, path=path, chunk_ids=ids,
            ).consume()
            counts["pruned"] += r.counters.nodes_deleted

        for c in chunks:
            sess.run(
                _UPSERT_CHUNK,
                id=c.id, repo=c.repo, file_path=c.file_path,
                text=c.text, embed_vec=c.embed_vec,
                token_count=c.token_count,
                line_start=c.line_start, line_end=c.line_end,
            ).consume()
            counts["chunks"] += 1
    return counts
