# AIForgeCrew

Autonomous AI dev-team pipeline. Todo ticket in Postgres → LangGraph routes it
through supervisor → planner → doer → feedback → learner → compile-green diff
+ PR on a dedicated branch + memory digest.

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

## Ports

| Port | Host | What |
|---|---|---|
| 1234 | MS | LM Studio (OpenAI-compat — LLMs + nomic embed) |
| 1235 | NUC | SSH tunnel → MS:1234 |
| 5432 | NUC | Postgres (aiforge) |
| 7474 / 7687 | NUC | Neo4j HTTP / bolt |
| 8799 | NUC | FastAPI |

## Models (2026-04-24)

| Role | Model | Size |
|---|---|---|
| planner / feedback / supervisor / learner | `qwen3.6-27b` | 16 GB |
| doer | `qwen3.6-35b-a3b@8bit` | 38 GB |
| MCP Qwen | `qwen3-coder-next` | shared ctx |
| embeddings | `nomic-embed-text-v1.5` (768d) | LM Studio |

Pinned 512K ctx + 12h TTL. Thinking disabled via
`chat_template_kwargs.enable_thinking=false`. `max_tokens=524288` everywhere.

## Key env

| Variable | Default | Purpose |
|---|---|---|
| `AIFORGE_DSN` | `postgresql://aiforge:aiforgepass@127.0.0.1:5433/aiforge` | Postgres |
| `AIFORGE_LM_BASE_URL` | `http://127.0.0.1:1234/v1` | LM Studio |
| `AIFORGE_{PLANNER,DOER,FEEDBACK,LEARNER}_MODEL` | see above | Per role |
| `AIFORGE_PLANNER_BACKEND` | `code` | `code` \| `toolcalling` |
| `AIFORGE_TICK_MAX_WALL` | `2400` | Max wall seconds per tick |
| `AIFORGE_GRAPH_MCP_ENABLED` | `0` | Opt-in: graph_rag MCP tools into smolagents |
| `AIFORGE_GRAPH_MCP_BIN` | unset | Local MCP binary (NUC co-located) |
| `AIFORGE_GRAPH_MCP_HOST` | `mani@192.168.70.191` | Remote SSH target (MS → NUC) |

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

## graph_rag (Neo4j + MCP)

All repos (Java/TS/Python) + k8s (qa+prod) + claude memories in one Neo4j.
25 MCP tools exposed to Claude Desktop and to smolagents Planner + Doer
(opt-in via `AIFORGE_GRAPH_MCP_ENABLED=1`).

- Auto-update: post-merge hooks on 43 NUC repos + timers every 5 min
- Full rebuild: `bash scripts/graph_rag/bin/graph_full_reindex.sh`
- Tools: `sym_lookup` · `impact` · `cross_repo_flow` · `ticket_brief` ·
  `caller_chain` · `build_plan` · `kube_status` · `find_doc` · 17 more

See [`docs/graph-rag.md`](./docs/graph-rag.md).

## Memory tiers

Postgres `memories` table with pgvector HNSW.

| Tier | Wing | Populated by | Lifetime |
|---|---|---|---|
| T1 | `ticket/<id>` | learner | ticket |
| T2 | `rules/*` | curated + planner | permanent |
| T3 | `skills/*` · `patterns/*` | planner + learner | permanent |
| T4 | `code/<repo>` | bulk / incremental reindex | rebuilt on commit |

Embed: nomic 768d (via LM Studio). Ranking: RRF over BM25 + vector.

## License

MIT
