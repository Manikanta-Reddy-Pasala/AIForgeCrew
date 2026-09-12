# aiforge-memory

The extraction half of AiForgeMemory, vendored into this repo so AIForgeCrew
resolves it from a path rather than a second git remote (see `[tool.uv.sources]`
in the root `pyproject.toml`).

It parses repositories and produces structures: packs, walked symbols, call
edges, file and symbol summaries, chunk embeddings, git metadata. It does **not**
contain the graph store, the CLI or the web UI — those live in the standalone
AiForgeMemory repo, which the NUC deploy clones separately. There is no
`aiforge-memory` console script in this package, and nothing here talks to Neo4j.

## Layout

```
core/state.py      sqlite ingest state — merkle_repo, merkle_files, service_overrides
features/
  repo/            RepoMix pack + LLM repo summary
  service/         LLM service extraction, with .aiforge/services.yaml overrides
  file/            per-file LLM summary
  symbol/          tree-sitter walk, call/import edges, symbol summaries
  chunk/           chunking (chonkie optional) + sidecar embeddings
  git_meta/        git metadata read
  lsp/             optional LSP-backed call resolution
query/
  fastpath.py      regex shortcut for explicit symbol / ticket / path queries
  bundle/          ContextBundle dataclass + helpers
ops/backup.py      state-db backup (VACUUM INTO) + log rotation
llm_compat.py      response_format shim across OpenAI / LM Studio / vLLM
```

Every node this package describes carries `SCHEMA_VERSION = "codemem-v1"`.

## Install and test

```bash
cd packages/aiforge_memory
make install            # uv venv .venv + uv pip install -e ".[dev]"
.venv/bin/pytest -q     # testpaths = aiforge_memory
```

Tests sit beside each feature in `features/<name>/tests/`, each with a README
covering fixtures and failure modes. Two markers gate the tests needing outside
help: `live_llm` (planner LLM at `AIFORGE_INTENT_LM_URL`) and `live_repomix`
(the `repomix` binary on PATH).

The `test-L1`..`test-L15` targets in `Makefile` point at an
`aiforge_memory/tests/L*/` layout this vendored copy does not have — use the
paths above.

## Environment

| Variable | Default |
|---|---|
| `AIFORGE_CODEMEM_STATE_DB` | `~/.aiforge/codemem.state.db` |
| `AIFORGE_CODEMEM_LM_URL` · `_MODEL` · `_KEY` | LM Studio; `qwen3.6-27b-instruct`; `lm-studio` |
| `AIFORGE_CODEMEM_RESPONSE_FORMAT` | `json_schema` — the only mode every backend accepts |
| `AIFORGE_CODEMEM_REPOMIX` | `repomix` on PATH |
| `AIFORGE_CODEMEM_PACK_MAX_CHARS` | pack truncation before the summary LLM |
| `AIFORGE_CODEMEM_FILE_SUMMARY_MAX_BYTES` | `32768` |
| `AIFORGE_EMBED_URL` · `_MODEL` · `_DIM` | `http://127.0.0.1:8764`; `bge-m3`; `1024` |
| `AIFORGE_SYMSUM_*` | Symbol-summary concurrency, timeouts, retries, size limits |
| `AIFORGE_CODEMEM_CHUNK_*` · `_DOC_*` | Chunk sizing for code and prose |
