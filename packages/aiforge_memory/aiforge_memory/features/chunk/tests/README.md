# chunk — tests

Covers `embed.chunk_and_embed()` (sliding-window chunks → sidecar embeddings),
`embed.embed_config()` and the optional chonkie adapter, which falls back to
line windows when chonkie is absent.

## Fixture

The shared `../../symbol/tests/fixtures/poly_repo/` tree. Embeddings come from
the bge-m3 sidecar at `AIFORGE_EMBED_URL`.

## Run

    pytest aiforge_memory/features/chunk/tests -v

## On failure

- Sidecar down → `curl $AIFORGE_EMBED_URL/healthz` (default
  `http://127.0.0.1:8764`) and confirm the model is actually loaded.
- Dimension mismatch → `AIFORGE_EMBED_DIM` (default 1024) must match what the
  sidecar serves; `AIFORGE_EMBED_MODEL` selects the model.
- chonkie tests skipping → install the extra: `pip install -e ".[chunking]"`.
  Code chunking needs no extra; it runs on the core tree-sitter dependency.
- Half-written vectors → a sidecar failure on any chunk is meant to halt the
  whole file rather than persist a partial set.
