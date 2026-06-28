# Layer L5 — chunk embeddings gate

## Purpose
After Stage 7, every supported file (Python/Java/TS, ≤ 64 KB, no parse error)
has been split into ~50-line chunks (10-line overlap) and each chunk has a
1024-d `embed_vec` from the bge-m3 sidecar (`/embed`). Chunks are linked to
their File_v2 via `CHUNKED_AS`. Stale chunks for re-ingested files are pruned.

## Fixture
- input: `aiforge_memory/tests/L4_symbols/fixtures/poly_repo/`
- sidecar: real bge-m3 at `AIFORGE_EMBED_URL` (default `http://127.0.0.1:8764`).

## Command

    make test-codemem-L5

or directly:

    pytest aiforge_memory/tests/L5_chunks_vectors/ -v

## Pass criteria
- Sliding-window chunks span the file with 10-line overlap
- Chunk IDs are deterministic (sha256 of `repo::path::idx`)
- Sidecar failure on any chunk halts the file (don't write half-vectors)
- Files > MAX (64 KB) skipped without raising
- Parse-error files skipped
- Vector dim = 1024 (bge-m3 default)
- `Chunk_v2` is uniquely keyed by `id`; vector index `codemem_chunk_embed`
  exists with `vector.dimensions=1024, vector.similarity_function='cosine'`

## On failure
- "sidecar down" → check `curl http://127.0.0.1:8764/healthz` and that
  bge-m3 is actually loaded
- vector index missing → `schema.apply(driver)` to recreate
- chunks orphaned (CHUNKED_AS missing) → check that File_v2 already exists
  before chunk upsert (Stage 4 must run before Stage 7)
- escalation: open ticket `CODEMEM-L5-<short>`
