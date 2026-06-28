# AiForgeMemory

KISS code memory layer for AiForge agents. Tree-sitter ingest → Neo4j graph → unified search/recall over symbols, files, services, chunks, and free-form memory facts.

```
aiforge_memory/
├── core/        Neo4j driver + schema + ingest state
├── features/    one folder per feature: extract → store → tests
│   ├── repo, service, file, symbol, chunk, memory
│   ├── link, delta, scheduler, lsp, git_meta
│   ├── flow, eval
│   └── external_ingest  ← gap-9 (2026-05-23) — generic text → Doc + chunks
├── query/       cross-feature read paths (translator, bundle, fastpath)
├── api/         CLI (cli.py) + HTTP read endpoints (http.py)
├── ui/          FastAPI UI server + index.html (Repos / Memory / Search / Links / Graph / Scheduler / Health tabs)
└── ops/         backup, health
```

## Install

```bash
uv sync
```

## CLI

```bash
aiforge-memory ingest <repo>                              # full ingest (tree-sitter walk → Neo4j)
aiforge-memory ingest-external <src> --repo <r>           # gap-9: file/http/raw → Doc + chunks
aiforge-memory schedule run                               # periodic ingest daemon
aiforge-memory remember <text>                            # write a memory fact
aiforge-memory recall <query>                             # vector recall (PPR-lite reranked, excludes superseded)
aiforge-memory recap <repo>                               # gap #5: human-readable digest of recent memory
aiforge-memory handoff <repo>                             # gap #5: portable JSON memory snapshot for a fresh session
aiforge-memory forget <repo> --id <id> --type <label>    # hard-delete one memory node
aiforge-memory ui --host 0.0.0.0 --port 8767
aiforge-memory health                                     # probe Neo4j + LM + sidecars
```

Full list: `aiforge-memory --help`.

## HTTP / UI

`aiforge-memory ui` serves the read-only web UI on http://host:8767.

| Route | Purpose |
|---|---|
| `GET /api/repos` | indexed repo list |
| `GET /api/repo/{name}` | per-repo summary |
| `GET /api/file?path=...` | file content + symbols |
| `POST /api/search` | hybrid vector + fulltext + graph-hop |
| `GET /api/memory` | list memory facts |
| `GET /api/links` | cross-repo CALLS_REPO edges |
| `GET /api/scheduler` | scheduled ingest state |
| `GET /api/health` | Neo4j + sidecar probes |
| `GET /api/graphify` | repos with graphify-out + metadata |
| `GET /api/graphify/{repo}/report` | GRAPH_REPORT.md |
| `GET /api/graphify/{repo}/graph` | graph.json |
| `GET /graphify/{repo}/wiki/{path}` | inline wiki tree |

## Memory feature surface (2026-05-23)

### Observation_v2 / Decision_v2 writes — `features/memory/store.py`

`upsert_observation(driver, *, repo, text, kind, tags, refs, embed_vec, media_refs, event_time, dedupe, supersedes)`

- **Exact-text dedupe (gap-3, PR #4)** — when `dedupe=True` (default) a `(repo, text)` match returns the existing node id with `seen_count` bumped and `last_seen_at` stamped. Tags are unioned (APOC), with a counter-only fallback for plain Neo4j Community. Return adds `deduped: bool`.
- **`supersedes` (gap #2, contradiction resolution)** — list of older Observation ids this fact replaces. Each is set `status='superseded'` with a `SUPERSEDES` edge from the new node; superseded nodes drop out of both vanilla `recall_observations` (now status-filtered) and the PPR reranker, so a corrected fact stops co-surfacing with the stale one. The AIForgeCrew learner wires this on semantic-near-dup (`SOFT ≤ sim < HARD`) — last-write-wins.
- **`media_refs` (gap-10)** — list of image / video / file paths or URLs round-tripped as a string array so a future vision-embed pipeline can fold image features into recall.
- **`event_time` (gap-7, bi-temporal)** — epoch seconds for the real-world time the fact refers to. Distinct from `created_at` (ingest moment). Defaults to ingest so old callers stay correct without a migration. Query layers can now hop by `event_time`.

### `recall_observations_ppr` (gap-6, PPR-lite reranker)

`recall_observations_ppr(driver, *, repo, query_vec, k=10, seed_k=25, alpha=0.6)`

Single-iteration personalized PageRank over the `(Observation_v2)-[:MENTIONS]->(File_v2|Symbol_v2)` neighbourhood without requiring the Neo4j GDS plugin. Vector recall picks seeds, 1-hop expansion finds shared neighbours, candidate observations score `alpha * vec_score + (1-alpha) * overlap_score`. `alpha=1.0` collapses to vanilla recall; `alpha=0.0` ranks purely by neighbour overlap.

Production `bundle._vector_observations` calls this first and falls back to vanilla vector recall if PPR errors, so every ticket's memory block now benefits.

### `external_ingest` (gap-9 spine)

`from aiforge_memory.features.external_ingest import ingest_external_source`

`ingest_external_source(driver, *, source, repo, source_type="external", title, tags, embed_fn)`

Resolves `source` as local file / `http(s)://` URL / raw text, chunks on blank-line boundaries (default 1200 chars via `AIFORGE_INGEST_CHUNK_CHARS`), and writes one `Doc_v2` parent + N `Note_v2` chunks with consistent tags (`source_type:`, `ingest_kind:`, `uri:`, `chunk_idx:`, `doc_id:`). Connectors (Confluence / Slack / Jira / Notion) layer on top of this spine — each fetches its raw text from that system's API or MCP tool and hands it here.

## Adding a feature

1. Create `aiforge_memory/features/<name>/` with `extract.py`, `store.py`, `tests/`.
2. Read from `core/neo4j.py` (driver) and `core/state.py` (ingest cursor).
3. Wire CLI entry in `api/cli.py` if user-facing.
4. Add HTTP endpoint in `api/http.py` or `ui/server.py` if browsable.

## Tests

```bash
uv run pytest aiforge_memory/ -q
```

180+ feature-tree tests including 14 memory_writer + 10 external_ingest. Neo4j-gated tests skip when no live `bolt://127.0.0.1:7687`.
