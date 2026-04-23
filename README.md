# AIForgeCrew

Autonomous AI dev-team pipeline driven by a LangGraph state machine. A todo ticket enters from Postgres; the graph routes it through supervisor, planner, doer, feedback, and learner nodes; a compile-green diff lands on a dedicated git branch; a digest is written back to memory.

**Runbook:** [`docs/runbook.md`](./docs/runbook.md)

---

## How it works

```
Ticket in Postgres (ONE-<n>)
   │
   ▼  launchd com.aiforge.graph-runner (60 s poll)
python -m aiforge_core.runtime
   │
   ├── tickets.claim_next_any()   → oldest todo ticket across all roles
   ├── graph_runner.run_graph()   → builds AgentState, invokes LangGraph graph
   │      supervisor_node         → rule-based routing, worktree setup
   │      planner_node            → _run_tool_loop (context + LLM tool loop)
   │      doer_node               → smolagents ToolCallingAgent (edit, compile, grep)
   │      feedback_node           → single-shot LLM verdict (pass/fail/scope_violation)
   │      learner_node            → single-shot LLM digest + T1 memory write
   └── ticket status updated; structured log appended
```

---

## Architecture

### State machine

One `StateGraph` compiled at startup (`aiforge_core/graph/graph.py`). Each invocation carries an `AgentState` typed dict through the nodes. LangGraph's `PostgresSaver` writes checkpoints keyed by `ticket.identifier` so interrupted runs can resume.

```mermaid
flowchart TD
    START([START]) --> S[supervisor_node]
    S -->|assignee_role=planner| P[planner_node]
    S -->|assignee_role=doer| D[doer_node]
    S -->|assignee_role=learner| L[learner_node]
    S -->|done/blocked| END([END])
    P --> D
    D -->|compile_fail_count >= 2| P
    D -->|normal| F[feedback_node]
    D -->|done/blocked| END
    F -->|verdict=pass| L
    F -->|verdict=fail, fail_count < 2| D
    F -->|verdict=scope_violation| END
    F -->|fail_count >= 2| END
    L --> END([END])
```

### Nodes

All nodes live in `aiforge_core/graph/nodes/`.

| Node | File | Behaviour |
|------|------|-----------|
| `supervisor_node` | `supervisor.py` | Rule-based only. Reads `ticket.assignee_role`, calls `_ensure_branch_and_worktree`, forwards state. No LLM call. |
| `planner_node` | `planner.py` | Calls `inject_context` then `_run_tool_loop` (legacy tool-loop path unchanged). |
| `doer_node` | `doer.py` | Calls `inject_context` then `run_smolagents_doer` (smolagents `ToolCallingAgent`). Falls back to `_run_tool_loop` only when worktree is None. |
| `feedback_node` | `feedback.py` | Single LLM call. Runs `git diff HEAD~1`, sends diff + ticket body to model, parses `{verdict, reason, fixlist}` JSON. |
| `learner_node` | `learner.py` | Single LLM call. Extracts a DIGEST line from recent events, writes T1 memory, posts a comment. |

### Routing edges

Defined in `aiforge_core/graph/edges.py`:

- **supervisor → planner/doer/learner/END**: maps `ticket.assignee_role` (`sr_developer` → planner, `developer` → doer, `fact_extract` → learner). Legacy aliases resolve transparently.
- **doer → feedback** (normal) or **doer → planner** (`compile_fail_count >= 2`).
- **feedback → learner** (`verdict=pass`), **feedback → doer** (`verdict=fail`, `feedback_fail_count < 2`), **feedback → END** (`scope_violation` or `fail_count >= 2`).
- **learner → END** (always).

### Entry point

`aiforge_core/runtime/__main__.py`:

```python
ticket = tickets.claim_next_any()   # oldest todo across all roles
if ticket:
    run_graph(ticket.id)            # graph_runner.py
```

The plist (`scripts/runtime/com.aiforge.graph-runner.plist`) fires this every 60 seconds.

---

## Tech stack

### LangGraph (orchestration)

`StateGraph` replaces the previous per-role launchd tick processes. The entire pipeline runs in one Python process per tick, with an explicit typed state shared between nodes. This gives deterministic edge routing, built-in PostgresSaver checkpointing (resume on crash), and a clear audit trail — the graph's `thread_id` is the ticket identifier.

### smolagents ToolCallingAgent (Doer)

The Doer node runs a `smolagents.ToolCallingAgent` via LiteLLM routed to LM Studio. The agent's tool set is intentionally narrow: `read_file`, `edit_block`, `run_compile`, `grep`, `list_dir`, `final_answer`. A `ScopeGuard` parsed from the ticket's `## Files` section blocks any write outside the allowlist. Max 15 steps.

### Hybrid retrieval over pgvector (RAG)

`aiforge_core/rag/retriever.py` queries the `memories` table directly via `store_v2` (bypassing LlamaIndex's `PGVectorStore` which hardcodes a `data_` table prefix incompatible with our schema). Per-role tier policies live in `aiforge_core/retrieval.py::ROLE_POLICIES`. The pipeline: per-tier BM25 + vector retrieval → RRF fusion → bge-reranker-v2-m3 sidecar rerank.

---

## Agent model table

| Node | Calls LLM? | Env var | Effective model |
|------|------------|---------|-----------------|
| supervisor_node | **no** (rule-based routing only) | `AIFORGE_SUPERVISOR_MODEL` (dead for this node) | n/a |
| planner_node | yes | `AIFORGE_PLANNER_MODEL` | `openai/gpt-oss-20b` (config.py default; no plist override) |
| doer_node | yes (via smolagents + LiteLLM) | `AIFORGE_DOER_MODEL` | `qwen3-coder-next` (config.py default; no plist override) |
| feedback_node | yes (single-shot) | `AIFORGE_FEEDBACK_MODEL` | `gemma-4-26b-a4b-it` (plist override) |
| learner_node | yes (single-shot) | `AIFORGE_LEARNER_MODEL` | `openai/gpt-oss-20b` (plist override) |

`AIFORGE_SUPERVISOR_MODEL` is still defined + overridden in the plist (`gemma-4-26b-a4b-it`) but the current `supervisor_node` never instantiates an LLM — it only reads `ticket.assignee_role` to route. Kept in config in case a future supervisor variant needs reasoning.

All models are served by local LM Studio at `http://localhost:1234/v1` (Mac Studio, M3 Ultra 96 GB). Override the base URL via `AIFORGE_LM_BASE_URL`.

---

## Git workflow (preserved from pre-migration)

The worktree and branch setup is unchanged. `_ensure_branch_and_worktree` in `aiforge_core/runtime/orchestrator.py` handles it; supervisor_node and doer_node both call it before any file work.

- Branch: `aiforge/<PARENT_TICKET_IDENT>-<slug>` (e.g. `aiforge/ONE-42-add-pagination`)
- Worktree: `<repo>/.aiforge-worktrees/<PARENT_TICKET_IDENT>/`
- All children of the same parent ticket share the branch and worktree.
- The doer's `edit_block` and `run_compile` tools operate inside the worktree. The scope guard enforces that writes stay within paths listed in the ticket's `## Files` section.
- **Doer auto-commits + pushes + opens a PR** when the agent calls `final_answer` with a non-empty diff and compile was green. Each step is fail-soft:
  - `git add -A && git commit` with `aiforge(<ID>): <title>` message + changed-file list + Co-Authored-By trailer.
  - `git push -u origin <branch>` — failure is logged, Doer still returns success for commit.
  - `gh pr create` — only if `gh` is on PATH **and** the `origin` URL contains `github.com`. Local-path origins (e.g. canary fixtures) skip PR creation with `doer.pr.skipped reason=origin_not_github`.
- Events emitted per step: `doer.commit.ok` / `.failed` / `.exception`, `doer.push.ok` / `.failed`, `doer.pr.ok` / `.failed` / `.skipped`. The ticket comment body also includes `PR: <url>` or `Commit: <sha>` so the UI surfaces the link.

---

## Quickstart

```bash
# 1. Clone and install
cd ~/AIForgeCrew
uv pip install -e .

# 2. Postgres schema (tickets + memories + LangGraph checkpoints)
psql aiforge < db/migrations/2026-04-21-tickets.sql
psql aiforge < db/migrations/2026-04-23-langgraph-checkpoints.sql

# 3. Install the single launchd plist (replaces all per-role plists)
bash scripts/runtime/install-launchd.sh

# 4. Verify it loaded
launchctl list | grep aiforge

# 5. Create a ticket
python -m aiforge_core.runtime.cli create \
  --title "Add pagination to /api/products" \
  --body "## Files\n- PosClientBackend/src/main/java/com/pos/backend/api/ProductsController.java\n## Acceptance\n- Returns page + size query params\n- Returns 200 with paginated JSON" \
  --assignee planner

# 6. Watch it run
tail -f ~/.aiforge/logs/graph-runner.log
```

The plist polls every 60 seconds. The first tick after ticket creation will claim it.

---

## Infrastructure topology (2026-04-23)

Three hosts. Mac Studio is LLM-only, NUC runs the stateful stuff,
laptop is for dev + UI.

```
Mac Studio 192.168.70.185          NUC 192.168.70.191 (static IP)   Laptop
---------------------------------  --------------------------------  ------------------
com.aiforge.lmstudio       (LLM)   aiforge-api.service  (FastAPI)    dev shell
com.aiforge.embed-sidecar  (LLM)   aiforge-file-indexer.timer (30m)  graph_rag queries
com.aiforge.graph-runner (orch.)   aiforge-reindex-daily.timer (02:00)
com.aiforge.caffeinate             aiforge-git-pull.timer  (10m — AIForgeCrew)
com.aiforge.pg-tunnel  (bridge)    aiforge-repo-pull.timer (5m — every ~/codeRepo + claude-memory)
                                   lm-tunnel.service (ssh -L to MS LM Studio)
                                   postgresql (aiforge db, pgvector+pg_trgm+pgcrypto)
                                   neo4j (docker, 4G heap, vector indexes)
```

- **No rsync anywhere.** All source comes from GitHub. `~/.claude/memory`
  lives at `github.com/Manikanta-Reddy-Pasala/claude-memory` (private);
  NUC + MS clone it, laptop pushes.
- **Bridges:** Mac Studio `com.aiforge.pg-tunnel` ssh `-L 5433:5432` to
  NUC Postgres (macOS Sequoia launchd sandbox workaround). NUC
  `lm-tunnel.service` ssh `-L 1235:1234` to MS LM Studio.

## Services and ports

| Port | What | Host | Owner |
|------|------|------|-------|
| 1234 | LM Studio | Mac Studio | `lms server` |
| 1235 | LM Studio (tunnel) | NUC localhost | systemd `lm-tunnel.service` |
| 5432 | Postgres (aiforge) | NUC | system postgresql |
| 5433 | Postgres (tunnel) | MS localhost | launchd `com.aiforge.pg-tunnel` |
| 7474 | Neo4j HTTP | NUC | docker `neo4j-aiforge` |
| 7687 | Neo4j Bolt | NUC | docker `neo4j-aiforge` |
| 8799 | FastAPI | NUC | systemd `aiforge-api.service` |
| 8764 | bge-m3 embed sidecar | Mac Studio | launchd `com.aiforge.embed-sidecar` |
| — | Graph-runner tick (60 s) | Mac Studio | launchd `com.aiforge.graph-runner` |
| — | NUC timers | NUC | systemd `aiforge-*.timer` |

---

## Configuration (environment variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `AIFORGE_DSN` | `postgresql://aiforge:aiforgepass@127.0.0.1:5433/aiforge` (MS via pg-tunnel) / `postgresql://aiforge:aiforgepass@127.0.0.1:5432/aiforge` (NUC direct) | Postgres DSN (tickets + memories + checkpoints) |
| `AIFORGE_LM_BASE_URL` | `http://127.0.0.1:1234/v1` (MS) / `http://127.0.0.1:1235/v1` (NUC via lm-tunnel) | LM Studio OpenAI-compat endpoint |
| `AIFORGE_LM_API_KEY` | `lm-studio` | API key (LM Studio accepts any value) |
| `AIFORGE_SUPERVISOR_MODEL` | `qwen3.6-35b-a3b` | Supervisor LLM (rule-based routing, model used for tie-break) |
| `AIFORGE_PLANNER_MODEL` | `qwen3.6-35b-a3b` | Planner LLM |
| `AIFORGE_PLANNER_BACKEND` | `code` | `code` \| `toolcalling` — smolagents backend |
| `AIFORGE_DOER_MODEL` | `qwen3-coder-next` | Doer LLM (code-focused) |
| `AIFORGE_FEEDBACK_MODEL` | `qwen3.6-35b-a3b` | Feedback verdict LLM |
| `AIFORGE_LEARNER_MODEL` | `qwen3.6-35b-a3b` | Learner LLM |
| `AIFORGE_NEO4J_URI` | `bolt://127.0.0.1:7687` | Neo4j (NUC-local) for graph-RAG |
| `AIFORGE_EMBED_URL` | `http://127.0.0.1:8764` | bge-m3 embed sidecar (MS) |
| `AIFORGE_TICK_MAX_WALL` | `2400` | Max wall seconds per graph invocation |
| `AIFORGE_MEMGUARD_DISABLE` | unset | Set to `1` to bypass RAM guard |

---

## Memory tiers

Single Postgres `memories` table with pgvector HNSW index.

| Tier | Wing pattern | Populated by | Lifetime |
|------|-------------|--------------|---------|
| T1 episodic | `ticket/<id>` | orchestrator + learner_node | ticket lifetime |
| T2 canon | `rules/canon`, `rules/*` | curated seeds + planner retain_fact | permanent |
| T3 skills/patterns | `skills/*`, `patterns/*` | planner + learner retain_fact | permanent |
| T4 code chunks | `code/<repo>` | bulk/incremental reindex scripts | rebuilt on post-commit hook |

Embeddings: bge-m3 (dim 1024, sidecar :8764). Rerank: bge-reranker-v2-m3 (sidecar :8765). Per-role retrieval policies: `aiforge_core/retrieval.py::ROLE_POLICIES`.

---

## What a successful run looks like (canary evidence)

ONE-CANARY-1 (pagination task similar to ONE-3):

1. `graph_runner.start` — ticket claimed, state initialized.
2. `supervisor.route` — worktree prepared, `assignee_role=doer` forwarded.
3. `smolagents.start` — `ToolCallingAgent` launched with qwen3-coder-next.
4. Agent: `read_file` → `edit_block` → `run_compile` (EXIT=0 in 2-3 iterations) → `final_answer`.
5. `smolagents.done` — diff non-empty, comment posted.
6. `feedback_node` — gemma-4-26b-a4b-it single-shot call, verdict=pass.
7. `learner_node` — digest written, T1 memory stored.
8. `graph_runner.done` — ticket status → done, wall_s ~120.

Worktree: `<repo>/.aiforge-worktrees/ONE-CANARY-1/`
Branch: `aiforge/ONE-CANARY-1-canary-smolagents-doer-smoke`

---

## Development

### Run tests

```bash
pytest
```

### Repository layout

```
aiforge_core/
├── graph/
│   ├── graph.py              # StateGraph definition + PostgresSaver wiring
│   ├── state.py              # AgentState TypedDict
│   ├── edges.py              # route_from_supervisor, after_doer, after_feedback
│   └── nodes/
│       ├── supervisor.py     # rule-based routing node
│       ├── planner.py        # _run_tool_loop wrapper
│       ├── doer.py           # run_smolagents_doer wrapper
│       ├── feedback.py       # single-shot verdict node
│       ├── learner.py        # single-shot digest node
│       └── retriever.py      # inject_context (calls rag.retriever)
├── doer/
│   ├── agent.py              # build_doer_agent (ToolCallingAgent factory)
│   ├── orchestrator_bridge.py # run_smolagents_doer (called by doer_node)
│   ├── tools.py              # read_file, edit_block, run_compile, grep, list_dir
│   └── scope_guard.py        # ScopeGuard + parse_allowed_files
├── rag/
│   └── retriever.py          # retrieve_for_role_li (store_v2 hybrid retrieval)
└── runtime/
    ├── __main__.py           # claim_next_any → run_graph (entry point)
    ├── graph_runner.py       # run_graph() — builds state, invokes graph, updates status
    ├── orchestrator.py       # _run_tool_loop, _ensure_branch_and_worktree, _write_t1_memory
    ├── config.py             # ROLES, model env vars, DSNs, ROLE_POLICIES aliases
    ├── tickets.py            # Postgres CRUD: claim_next_any, create, get, add_event
    ├── retrieval.py          # ROLE_POLICIES, rrf_fuse, Hit
    ├── store_v2.py           # Store: search_tier_vec, search_tier_bm25, upsert_memory
    ├── embed.py              # embed() → bge-m3 sidecar :8764
    ├── memory.py             # Memory.search, Memory.retain_fact
    └── logging_setup.py      # emit() structured ndjson logger

db/migrations/
├── 2026-04-21-tickets.sql           # tickets + ticket_events + counter
└── 2026-04-23-langgraph-checkpoints.sql  # checkpoints + checkpoint_blobs + checkpoint_writes

scripts/runtime/
├── install-launchd.sh               # idempotent LaunchAgent installer
├── com.aiforge.graph-runner.plist   # single graph-runner timer (60 s)
├── com.aiforge.pg-watchdog.plist    # postgres health watchdog
├── com.aiforge.git-pull.plist       # periodic git pull
├── com.aiforge.file-indexer.plist   # incremental file indexer
└── com.aiforge.reindex-daily.plist  # daily memory reindex @ 02:00

docs/
├── runbook.md
└── migration/
    └── 2026-04-22-langgraph-llamaindex-smolagents.md
```

### Adding a new node

1. Create `aiforge_core/graph/nodes/your_node.py` following the signature `def your_node(state: AgentState) -> AgentState`.
2. Register it in `aiforge_core/graph/graph.py` via `builder.add_node(...)`.
3. Add routing logic in `aiforge_core/graph/edges.py`.
4. If it needs a new model env var, add it to `config.py` and to the plist's `EnvironmentVariables` dict.

---

## Operations

### launchctl commands

```bash
# Check the runner is loaded
launchctl list | grep aiforge

# Restart the graph-runner (picks up env var changes)
launchctl kickstart -k gui/$(id -u)/com.aiforge.graph-runner

# Stop all aiforge agents
launchctl bootout gui/$(id -u)/com.aiforge.graph-runner
launchctl bootout gui/$(id -u)/com.aiforge.pg-watchdog
launchctl bootout gui/$(id -u)/com.aiforge.reindex-daily

# Reinstall (after plist changes)
bash ~/AIForgeCrew/scripts/runtime/install-launchd.sh
```

### Log locations

| Log | Path |
|-----|------|
| Graph-runner stdout | `~/.aiforge/logs/graph-runner.log` |
| Graph-runner stderr | `~/.aiforge/logs/graph-runner.err` |
| Orchestrator structured ndjson | `~/.aiforge/logs/orchestrator-*.ndjson` |

```bash
tail -f ~/.aiforge/logs/graph-runner.log
tail -f ~/.aiforge/logs/orchestrator-*.ndjson | jq -c '{ts,role,ticket,event,dur_ms}'
```

### Reset a stuck ticket

```sql
-- Unblock a ticket stuck in in_progress
UPDATE tickets SET status = 'todo', updated_at = now()
WHERE identifier = 'ONE-123';
```

---

## What was removed in the migration (commit 31a2bf8)

- 5+ per-role launchd plists (`com.aiforge.tick-supervisor`, `tick-planner`, `tick-planner-b`, `tick-doer`, `tick-doer-b`, `tick-feedback`, `tick-learner`)
- `orchestrator.py::tick(role)` — replaced by `graph_runner.run_graph`
- `tickets.claim_next(role)` — replaced by `tickets.claim_next_any()`
- `retrieval.py::retrieve_for_role` — replaced by `rag.retriever.retrieve_for_role_li`
- `aiforge_core/runtime/feature_flags.py`

The legacy orchestrator functions (`_run_tool_loop`, `_ensure_branch_and_worktree`, `_write_t1_memory`) remain in `orchestrator.py` and are imported by the graph nodes directly.

---

## License

See `LICENSE`.
