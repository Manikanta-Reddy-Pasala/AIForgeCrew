# AIForgeCrew

Autonomous AI dev-team pipeline. Todo ticket in Postgres → LangGraph routes it
through supervisor → planner → doer → feedback → learner → compile-green diff
+ PR on a dedicated branch + memory digest.

## Docs

| Read | For |
|---|---|
| [`docs/architecture.md`](./docs/architecture.md) | Hosts, services, ports, bridges |
| [`docs/graph-rag.md`](./docs/graph-rag.md) | Neo4j graph + vector DB, MCP tools |
| [`docs/ticket-flow.md`](./docs/ticket-flow.md) | What happens when a ticket arrives |
| [`docs/runbook.md`](./docs/runbook.md) | Ops runbook |

## Topology (one-liner)

```
Mac Studio 192.168.70.185          NUC 192.168.70.191 (static)
LLMs + graph-runner + embeds       Postgres + Neo4j + API + indexers
```

Direct LAN 10.10.10.x between them. Source of truth: GitHub. No rsync.

## Ports

| Port | Host | What |
|------|------|------|
| 1234 | MS | LM Studio (OpenAI-compat) |
| 8764 | MS | bge-m3 embed sidecar |
| 5432 | NUC | Postgres (aiforge) |
| 7474 / 7687 | NUC | Neo4j HTTP / bolt |
| 8799 | NUC | FastAPI |

## Models (2026-04-24)

| Role | Model | Size |
|---|---|---|
| planner / feedback / supervisor / learner | `qwen3.6-27b` (dense MLX 4-bit) | 16 GB |
| doer | `qwen3.6-35b-a3b@8bit` (MoE MLX 8-bit, coding-tuned) | 38 GB |

Both pinned at 512K ctx + 12 h TTL. Qwen3.6 is a reasoning model — we
disable thinking via `chat_template_kwargs.enable_thinking=false` so
`message.content` actually gets populated. `max_tokens=524288` everywhere
(cap, not floor).

## Key env

| Variable | Default | Purpose |
|---|---|---|
| `AIFORGE_DSN` | `postgresql://aiforge:aiforgepass@127.0.0.1:5433/aiforge` (MS) | Postgres |
| `AIFORGE_LM_BASE_URL` | `http://127.0.0.1:1234/v1` | LM Studio |
| `AIFORGE_PLANNER_MODEL` / `AIFORGE_DOER_MODEL` / `_FEEDBACK_` / `_LEARNER_` | see above | Model id per role |
| `AIFORGE_PLANNER_BACKEND` | `code` | `code` \| `toolcalling` |
| `AIFORGE_TICK_MAX_WALL` | `2400` | Max wall seconds per tick |

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

## Memory tiers

Single Postgres `memories` table with pgvector HNSW.

| Tier | Wing | Populated by | Lifetime |
|------|------|--------------|---------|
| T1 | `ticket/<id>` | learner | ticket |
| T2 | `rules/*` | curated + planner | permanent |
| T3 | `skills/*`, `patterns/*` | planner + learner | permanent |
| T4 | `code/<repo>` | bulk / incremental reindex | rebuilt on commit |

Embed: bge-m3 1024-d. Rerank: bge-reranker-v2-m3.

## License

MIT
