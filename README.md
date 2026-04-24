# AIForgeCrew

Autonomous AI dev-team pipeline. Todo ticket in Postgres → LangGraph routes it
through supervisor → planner → doer → feedback → learner → compile-green diff
+ auto-commit + push on a dedicated branch + Neo4j memory digest.

## Docs

| Read | For |
|---|---|
| [`docs/architecture.md`](./docs/architecture.md) | Hosts, services, ports, bridges |
| [`docs/stack.md`](./docs/stack.md) | Tools + libraries + why two machines |
| [`docs/graph-rag.md`](./docs/graph-rag.md) | Neo4j graph + vector DB, MCP tools, smolagents wiring |
| [`docs/auto-update.md`](./docs/auto-update.md) | Incremental reindex timers + hooks |
| [`docs/ticket-flow.md`](./docs/ticket-flow.md) | What happens when a ticket arrives |
| [`docs/runbook.md`](./docs/runbook.md) | Ops runbook |

## Topology

```
Mac Studio 192.168.70.185          NUC 192.168.70.191 (static)
LLM + graph-runner + embeds        Postgres + Neo4j + API + indexers + graph_rag MCP
```

Direct LAN 10.10.10.x. GitHub is source of truth. No rsync between hosts.
All MS → NUC traffic over SSH tunnels (macOS Sequoia launchd sandbox blocks
direct non-loopback connect from LaunchAgents).

## Ports

| Port | Host | What |
|---|---|---|
| 1234 | MS | LM Studio (OpenAI-compat — LLMs) |
| 1235 | NUC | SSH tunnel → MS:1234 |
| 5433 | MS | SSH tunnel → NUC:5432 Postgres |
| 7688 | MS | SSH tunnel → NUC:7687 Neo4j bolt |
| 7474 / 7687 | NUC | Neo4j HTTP / bolt |
| 8764 | MS | bge-m3 embed sidecar (1024d) |
| 8799 | NUC | FastAPI (tickets + events) |

## Models

Per-agent provider + model is config-driven via `agent_config.json` on the API
host (edit in the UI at `/settings`). Three providers today:

| Provider | Where | Auth env |
|---|---|---|
| `local` | LM Studio MLX on Mac Studio (`:1234` local / `:1235` SSH-tunneled on NUC) | — |
| `anthropic` | Anthropic Claude via LiteLLM | `ANTHROPIC_API_KEY` |
| `ollama_cloud` | Ollama Cloud | `OLLAMA_CLOUD_API_KEY` |

Default everything routes to `local` / `gpt-oss-120b` (63 GB MXFP4 GGUF, 32K
ctx — 128K crushed Mac Studio's 96 GB unified memory, 32K holds ~70% headroom).

| Role | Default |
|---|---|
| supervisor / planner / doer / feedback / learner / chat | `local` · `gpt-oss-120b` |
| embeddings | `bge-m3` (1024d, via sidecar :8764) |

Env vars `AIFORGE_{ROLE}_MODEL` / `AIFORGE_{ROLE}_PROVIDER` override the
persisted config when set — useful for one-off debug runs.

### Auto-pin watchdog

`com.aiforge.lms-pin` polls `lms ps` every 60s and re-pins the active local
model when the context drifts (LM Studio JIT-loads at 4K on bare-name API
requests and silently replaces the pinned instance). Self-heals across reboots,
consumer restarts, and JIT races. Script: `scripts/runtime/lms-pin-watchdog.sh`.

## Memory — Neo4j (Option A, 2026-04-24)

All agent memory lives in Neo4j as `(:Memory)` nodes with a 1024-d `bge-m3`
embedding in a native vector index + BM25 fulltext. RRF fuses both rankings.
Postgres stays only for workflow state (`tickets`, `ticket_events`,
`checkpoints`).

| Tier | Wing | Populated by | Lifetime |
|---|---|---|---|
| T1 | `ticket/<id>` | learner digest at end of each run | ticket |
| T2 | `rules/*`, `repo/<name>` | curated + repo catalog indexer | permanent |
| T3 | `skills/*`, `patterns/*` | planner + learner | permanent |
| T4 | `code/<repo>` | bulk / incremental reindex | rebuilt on commit |

Switch: `AIFORGE_MEMORY_BACKEND=neo4j` (default). `postgres` rollback still
supported — schema lives but is not written to.

### Repo catalog (2026-04-24)

`(:Repo {name, lang, stack, entry_cmd, compile_cmd, ports, dockerfile,
readme_sha, overview, last_seen_at})`. Indexer walks `~/codeRepo/*`, detects
Java / Python / Node stack (pom.xml with parent-chain recursion,
requirements.txt, package.json), and upserts a T2 `:Memory` twin per repo so
vector + BM25 retrieval reach it from any query. Refresh every 15 min via
`com.aiforge.repo-indexer`.

Planner calls `lookup_repo(project)` as the mandatory first step — grounds
Stack / Run sections in real repo config instead of stale ticket text.

## UI (`/ui/`)

Polished React+Vite dashboard at `https://77.42.45.12:9443/ui/`
(basic-auth behind nginx). Lazy-loaded routes, light theme, design-token CSS.

| Route | What |
|---|---|
| `/` Dashboard | Metric cards + sparklines + recent activity |
| `/board` Kanban | 6-column drag-and-drop (@dnd-kit) + priority-colored cards |
| `/tickets` | List + filter + create + detail |
| `/chat` | Memory-grounded agent Q&A — smolagents CodeAgent with 128 MCP tools, T1-T4 retrieval, typo-normalize pre-pass, **✓ Worked / ✘ Didn't help** footers persist Q+A as T3 patterns for next time |
| `/tools` | Direct invocation of all 25 graph_rag MCP tools |
| `/memory` | Semantic search over `:Memory` nodes |
| `/agents` | Per-role status + tool chips |
| `/logs` | SSE live tail of graph-runner ndjson |
| `/settings` | **Per-agent provider + model picker** (local / anthropic / ollama_cloud) |

## Chat agent — tool fleet

The `/api/chat/ask` endpoint runs a smolagents `CodeAgent` with live access to
five MCP servers, 128 tools total:

| Server | Transport | Tools |
|---|---|---|
| `graph_rag` (our own) | stdio | 25 — `sym_lookup`, `impact`, `cross_repo_flow`, `caller_chain`, `read_source`, `ticket_brief`, `related_memories`, `find_doc`, … |
| `oneshell_mongo` | streamable-http :8810 | 33 — `find`, `aggregate`, `find_business`, `find_sync_errors`, `find_crlf_dup_coas`, `dedupe_crlf_coas`, … |
| `oneshell_k8s` | streamable-http :8811 | 22 — `list_pods`, `rollout_restart`, `scale`, `start_port_forward`, `logs`, … |
| `oneshell_tekton` | streamable-http :8812 | 16 — `list_pipelineruns`, `pipelinerun_logs`, `trigger_release`, `get_qa_deployment`, … |
| `oneshell_tally` | streamable-http :8813 | 31 — `tc_status`, `tb_reconcile`, `diagnose_series_gap`, `run_all_rules`, … |
| `search_memory` | in-process | 1 |

Duplicate tool names are auto-prefixed with the server short name
(`mongo_list_services`, `k8s_list_services`) so every tool keeps a unique
callable handle.

## Key env

| Variable | Default | Purpose |
|---|---|---|
| `AIFORGE_DSN` | `postgresql://aiforge:aiforgepass@127.0.0.1:5433/aiforge` | Postgres (tickets/events/checkpoints) |
| `AIFORGE_MEMORY_BACKEND` | `neo4j` | `neo4j` \| `postgres` |
| `AIFORGE_NEO4J_URI` | `bolt://127.0.0.1:7688` | Neo4j bolt (via tunnel from MS) |
| `AIFORGE_NEO4J_USER` / `_PASSWORD` | `neo4j` / `password` | Neo4j auth |
| `AIFORGE_EMBED_URL` | `http://127.0.0.1:8764` | bge-m3 sidecar |
| `AIFORGE_EMBED_DIM` | `1024` | Vector index dim |
| `AIFORGE_LM_BASE_URL` | `http://127.0.0.1:1234/v1` | LM Studio |
| `AIFORGE_{PLANNER,DOER,FEEDBACK,LEARNER,SUPERVISOR}_MODEL` | see above | Per role |
| `AIFORGE_PLANNER_BACKEND` | `code` | `code` \| `toolcalling` |
| `AIFORGE_CODE_ROOT` | `~/codeRepo` | Root scanned by repo catalog |
| `AIFORGE_TICK_MAX_WALL` | `2400` | Max wall seconds per tick |
| `AIFORGE_GRAPH_MCP_ENABLED` | `0` | Opt-in: graph_rag MCP tools into smolagents |

Full list: `aiforge_core/runtime/config.py`.

## Quickstart (laptop)

```bash
make install        # .venv + deps
make test           # pytest
make status         # live API + tickets on NUC
make health         # /api/health
```

File a ticket:

```bash
curl -X POST http://192.168.70.191:8799/api/tickets \
  -H 'Content-Type: application/json' \
  -d '{"title":"...", "body":"... ## Files\n- path/to/file", "assignee_role":"planner"}'
```

## Pipeline features

- **Auto-commit + push on pass**: graph_runner commits Doer's worktree edits
  and pushes to `aiforge/<ticket>-*` branch when feedback verdict=pass. Before
  this, edits were silently wiped by worktree cleanup.
- **Build-artifact exclusion**: feedback diff pathspecs exclude
  `.flattened-pom.xml`, `target/`, `__pycache__/`, `*.pyc` so Maven's flatten
  plugin etc. don't trigger false scope_violations.
- **Already-implemented short-circuit**: feedback returns verdict=pass without
  LLM call when diff is empty AND target file already contains every `##`
  heading from the ticket's acceptance criteria. Prevents blocked-loop on
  tickets that ran once and landed correctly.
- **Path-prefix stripping**: Doer tools (read_file, edit_block, grep,
  list_dir) strip a leading `<repo>/` from Planner-written paths so
  `TallyConnector/README.md` resolves correctly inside the worktree.

## graph_rag (Neo4j + MCP)

All repos (Java/TS/Python) + k8s (qa+prod) + claude memories in one Neo4j.
25 MCP tools exposed to Claude Desktop and to smolagents Planner + Doer
(opt-in via `AIFORGE_GRAPH_MCP_ENABLED=1`).

- Auto-update: post-merge hooks on 43 NUC repos + timers every 5 min
- Full rebuild: `bash scripts/graph_rag/bin/graph_full_reindex.sh`
- Tools: `sym_lookup` · `impact` · `cross_repo_flow` · `ticket_brief` ·
  `caller_chain` · `build_plan` · `kube_status` · `find_doc` · 17 more

See [`docs/graph-rag.md`](./docs/graph-rag.md).

## Mac Studio LaunchAgents

| Label | Role |
|---|---|
| `com.aiforge.lmstudio` | LM Studio server :1234 |
| `com.aiforge.lms-pin` | Re-pin models at 512K ctx |
| `com.aiforge.graph-runner` | Ticket pipeline tick every 60s |
| `com.aiforge.repo-indexer` | Catalog refresh every 900s |
| `com.aiforge.embed-sidecar` | bge-m3 embed server :8764 |
| `com.aiforge.pg-tunnel` | SSH tunnel :5433 → NUC:5432 |
| `com.aiforge.neo4j-tunnel` | SSH tunnel :7688 → NUC:7687 |
| `com.aiforge.caffeinate` | Prevent sleep |

## License

MIT
