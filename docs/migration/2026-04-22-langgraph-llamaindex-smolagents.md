# Migration Plan: LangGraph + LlamaIndex + smolagents
**Date:** 2026-04-22  
**Status:** Planning — no code changed  
**Author:** Winston (AIForgeCrew Architect)

---

## 0. Guiding constraints

- Three feature flags, independently flippable: `doer.backend`, `rag.backend`, `orchestrator.backend`
- Phases ordered most-contained → least: Doer first, RAG second, Orchestrator third
- Each phase ships behind its flag; prior phase must be stable before next starts
- Supervisor / Planner / Feedback / Learner prompts and logic are **preserved verbatim** — only the harness around them changes

---

## 1. Current-state map

### 1.1 Role → file → function today

| Role | Entry point | Core functions | Postgres state read | Postgres state written |
|------|-------------|----------------|---------------------|------------------------|
| **Supervisor** | `aiforge_core/runtime/orchestrator.py:tick("supervisor")` | `_run_tool_loop`, `_finalize_ticket`, `_route_to_feedback` | `tickets.claim_next("supervisor")` → row lock; `ticket_events` tail | `tickets.update_status`, `ticket_events.add_event` |
| **Planner** | `orchestrator.py:tick("planner")` | `_run_tool_loop`, `_build_context_bundle`, `_linked_tickets_block` | `tickets.claim_next("planner")`; reads `memories` via `Memory.search` | `tickets.create` (children), `ticket_events.add_event`, `memories.retain_fact` |
| **Doer** | `orchestrator.py:tick("doer")` | `_run_tool_loop`, `_ensure_branch_and_worktree`, `_compact_old_tool_results` | `tickets.claim_next("doer")`; `memories` via search; `ticket.metadata.feedback_fixlist` | git worktree; `ticket_events`; `tickets.update_status` → `in_review`; triggers `_route_to_feedback` |
| **Feedback** | `orchestrator.py:tick("feedback")` | `_run_tool_loop`, `_finalize_ticket` (implicit-pass fallback) | `tickets.claim_next("feedback")`; diff via `run_shell(git show)` | `tickets.update_status` → `in_review`/`todo`; `ticket_events`; creates Learner ticket on pass |
| **Learner** | `orchestrator.py:tick("learner")` | `_run_tool_loop`, `_write_t1_memory` | `tickets.claim_next("learner")`; `ticket.body` DIGEST | `memories.retain_fact` (t3); `ticket_events`; `tickets.update_status("done")` |

### 1.2 Shared infrastructure

| Concern | Current file | Key class / function |
|---------|-------------|----------------------|
| LLM transport | `aiforge_core/runtime/llm.py` | `complete(role_cfg, messages, tools) → AssistantTurn`; backends: `openai` (LM Studio) + `claude_cli` |
| Tool catalogue | `aiforge_core/runtime/tools.py` | `@register`, `dispatch`, `ToolContext`, `ToolResult` |
| Role prompts | `aiforge_core/runtime/roles.py` | `SUPERVISOR_SYSTEM`, `PLANNER_SYSTEM`, `DOER_SYSTEM`, `FEEDBACK_SYSTEM`, `LEARNER_SYSTEM`; `build_messages()` |
| Ticket CRUD | `aiforge_core/runtime/tickets.py` | `claim_next`, `create`, `update_status`, `add_event`, `children` |
| Memory read | `aiforge_core/runtime/memory.py` | `Memory.search` → `retrieve_for_role` (BM25+vec+RRF+rerank) |
| Memory write | `aiforge_core/runtime/memory.py` | `Memory.retain_fact` → `Store.upsert_memory` |
| Embeddings | `aiforge_core/embed.py` | `embed(text)` → bge-m3 sidecar :8764 |
| Retrieval | `aiforge_core/retrieval.py` | `retrieve_for_role`, `rrf_fuse`, `rerank_http`; `ROLE_POLICIES` dict |
| Store | `aiforge_core/store_v2.py` | `Store.search_tier_bm25`, `search_tier_vec`, `upsert_memory` |
| Tick runner | `aiforge_core/runtime/__main__.py` | `python -m aiforge_core.runtime <role>` |
| Scheduling | `scripts/runtime/com.aiforge.tick-<role>.plist` | launchd timer per role, 60–120 s |
| RAM guard | `aiforge_core/runtime/memguard.py` | `enforce_ram_ceiling`, `ensure_loaded`, `plan_rebalance` |

### 1.3 Postgres schema (tickets)

```sql
-- tickets table (single source of truth for work state)
tickets(id, identifier, title, body, status, priority, assignee_role,
        parent_id, branch, project, labels, metadata, created_at, updated_at)

-- metadata JSONB carries: reclaim_count, last_stop_reason, feedback_fixlist,
--   max_turns override, auto_queued_by, feedback_verdict, last_blocked_reason

-- ticket_events(id, ticket_id, agent_role, kind, body, metadata, created_at)
-- kinds: comment | status_change | tool_call | error | llm_turn | retain | child_created
```

Ticket state machine: `todo → in_progress → in_review → done`  
Failure exits: `→ blocked | cancelled`. Reclaim resets `in_progress → todo` (max 3×).

---

## 2. Target-state map

### 2.1 LangGraph graph topology

```
            ┌─────────────┐
 ticket ──► │  Supervisor  │ ─── route ──► Planner ──► Doer ──► Feedback ──► Learner
            │   (node)     │                                │ compile_fail×2   │
            └─────────────┘                   ◄─ escalate ─┘                  │
                   ▲                                                           │ verdict_pass
                   └───────────────────── supervisor_help ────────────────────►│
                                                                               ▼
                                                                             [done]
```

**State schema class:** `aiforge_core/graph/state.py::AgentState`

```python
class AgentState(TypedDict):
    ticket_id: int
    ticket: dict           # snapshot of tickets row
    role: str              # current active node
    messages: list[dict]   # full message history (LangGraph channel)
    tool_results: list[dict]
    worktree_path: str | None
    stop_reason: str | None
    compile_fail_count: int   # Doer escalation counter
    verdict: str | None       # "pass" | "fail"
    feedback_fixlist: str | None
    learner_digest: str | None
    flags: dict               # feature flag snapshot at graph-entry time
```

### 2.2 Node → new file mapping

| LangGraph Node | New file | Behaviour preserved from |
|----------------|----------|--------------------------|
| `supervisor_node` | `aiforge_core/graph/nodes/supervisor.py` | `orchestrator.py:_run_tool_loop` (role=supervisor) + `roles.SUPERVISOR_SYSTEM` |
| `planner_node` | `aiforge_core/graph/nodes/planner.py` | same, role=planner + `_build_context_bundle` |
| `doer_node` | `aiforge_core/graph/nodes/doer.py` | **smolagents ToolCallingAgent** (Phase 1); falls back to legacy loop when `flags["doer.backend"]=="legacy"` |
| `feedback_node` | `aiforge_core/graph/nodes/feedback.py` | same, role=feedback + implicit-pass fallback |
| `learner_node` | `aiforge_core/graph/nodes/learner.py` | same, role=learner + `_write_t1_memory` |
| `retriever_node` | `aiforge_core/graph/nodes/retriever.py` | **LlamaIndex** RAG (Phase 2); falls back to `retrieve_for_role` when `flags["rag.backend"]=="legacy"` |

### 2.3 Edges and conditional functions

| Edge | Conditional function | File |
|------|---------------------|------|
| `supervisor → {planner, doer, learner}` | `route_from_supervisor(state)` — reads `state["ticket"]["assignee_role"]` written by supervisor's `update_assignee` tool | `aiforge_core/graph/edges.py` |
| `doer → feedback` | `after_doer(state)` — always routes to feedback | same |
| `doer → planner` (escalation) | `after_doer(state)` — when `state["compile_fail_count"] >= 2` | same |
| `feedback → learner` | `after_feedback(state)` — when `state["verdict"] == "pass"` | same |
| `feedback → doer` | `after_feedback(state)` — when `state["verdict"] == "fail"` | same |
| `feedback → supervisor` | `after_feedback(state)` — when `state["verdict"] == "scope_violation"` → abort path | same |
| `* → END` | any node sets `stop_reason="done"` or `"blocked"` | LangGraph `END` |

### 2.4 New file tree (target)

```
aiforge_core/
├── graph/
│   ├── __init__.py
│   ├── state.py              # AgentState TypedDict
│   ├── edges.py              # all conditional edge functions
│   ├── graph.py              # StateGraph assembly + compile()
│   └── nodes/
│       ├── __init__.py
│       ├── supervisor.py
│       ├── planner.py
│       ├── doer.py           # smolagents ToolCallingAgent wrapper
│       ├── feedback.py
│       ├── learner.py
│       └── retriever.py      # LlamaIndex retriever tool node
├── doer/
│   ├── __init__.py
│   ├── agent.py              # smolagents ToolCallingAgent factory
│   ├── tools.py              # read_file, edit_block, run_compile, grep, list_dir, final_answer
│   └── scope_guard.py        # allowlist wrapper on write tools
├── rag/
│   ├── __init__.py
│   ├── index.py              # LlamaIndex VectorStoreIndex over pgvector
│   ├── retriever.py          # hybrid BM25+vector, RRF, rerank via LlamaIndex
│   └── ingestion.py          # chunking + upsert pipeline (replaces store_v2 write path)
└── runtime/
    ├── feature_flags.py      # NEW: read/write flags from Postgres or env
    └── ... (existing files unchanged)
```

---

## 3. Migration phases

---

### Phase 1 — Doer swap to smolagents ToolCallingAgent

**Goal:** Replace the Doer's `_run_tool_loop` with a smolagents ToolCallingAgent behind `doer.backend` flag. All other roles untouched. Orchestrator harness (launchd ticks, Postgres claim/finalize) stays as-is.

#### New files to create

| Path | Purpose |
|------|---------|
| `aiforge_core/runtime/feature_flags.py` | `get_flag(name, default)` — reads from `AIFORGE_FLAG_<NAME>` env or Postgres `config` table; used by all phases |
| `aiforge_core/doer/__init__.py` | package marker |
| `aiforge_core/doer/scope_guard.py` | wraps write tools with allowlist check; raises `ScopeViolation` on path not in `## Files` section |
| `aiforge_core/doer/tools.py` | smolagents `Tool` subclasses: `ReadFileTool`, `EditBlockTool`, `RunCompileTool`, `GrepTool`, `ListDirTool`, `FinalAnswerTool` |
| `aiforge_core/doer/agent.py` | `build_doer_agent(ticket, worktree_path, context_bundle) → ToolCallingAgent`; wires tools + system prompt from `roles.DOER_SYSTEM`; `max_steps=15`, `num_retries=1` |
| `tests/python/test_doer_agent.py` | Unit tests: scope guard rejects out-of-scope paths; `EditBlockTool` find/replace happy path; `FinalAnswerTool` terminates loop; compile failure increments counter |

#### Existing files to modify

| File | Change |
|------|--------|
| `aiforge_core/runtime/orchestrator.py:tick()` | After `worktree = _ensure_branch_and_worktree(ticket)`, check `get_flag("doer.backend", "legacy")`. If `"smolagents"` and `role_name == "doer"`: call `run_smolagents_doer(ticket, worktree, log)` instead of `_run_tool_loop`. Preserve all finalize/reclaim logic after. |
| `aiforge_core/runtime/orchestrator.py` | Add `run_smolagents_doer(ticket, worktree_path, log) → dict` function. Builds context bundle, calls `build_doer_agent`, runs `.run(task=ticket.body)`, maps `FinalAnswerTool` output back to `{stop_reason, has_commented, turns, wall_s}` summary dict so `_finalize_ticket` works unchanged. |
| `aiforge_core/runtime/config.py` | No change to `ROLES`. Add import guard comment noting smolagents path skips `RoleConfig.max_turns` (replaced by `max_steps=15`). |
| `pyproject.toml` | Add `smolagents>=1.13` to `dependencies` (see §5) |

#### Tests to add

- `tests/python/test_doer_agent.py::test_scope_guard_blocks_outside_path`
- `tests/python/test_doer_agent.py::test_edit_block_happy_path`
- `tests/python/test_doer_agent.py::test_final_answer_stops_loop`
- `tests/python/test_doer_agent.py::test_compile_fail_propagates`

#### Feature flag state at end of Phase 1

```
doer.backend = legacy       # default — no behaviour change until explicitly flipped
rag.backend  = legacy       # untouched
orchestrator.backend = legacy  # untouched
```

To activate: `AIFORGE_FLAG_DOER_BACKEND=smolagents` in the `com.aiforge.tick-doer.plist` env.

#### Rollback

Set `AIFORGE_FLAG_DOER_BACKEND=legacy` (or remove env var). No DB migration needed. The smolagents path is a conditional branch; legacy path is still fully functional.

---

### Phase 2 — RAG swap to LlamaIndex

**Goal:** Replace `aiforge_core/retrieval.py` + `memory.py` search path with LlamaIndex hybrid retrieval over the existing pgvector `memories` table. Write path (`retain_fact`) stays on `store_v2` initially. Feature flag: `rag.backend`.

#### New files to create

| Path | Purpose |
|------|---------|
| `aiforge_core/rag/__init__.py` | package marker |
| `aiforge_core/rag/index.py` | `build_index(dsn, tier_filter) → VectorStoreIndex` using `LlamaIndexPGVectorStore` pointed at `memories` table; bge-m3 embed model via `HuggingFaceEmbedding` or custom `OpenAIEmbedding` subclass hitting :8764 sidecar |
| `aiforge_core/rag/retriever.py` | `retrieve_for_role_li(store, role, query, parent_id) → list[Hit]`; assembles `VectorIndexRetriever` + `BM25Retriever` per tier per `ROLE_POLICIES`; fuses with `QueryFusionRetriever` (RRF mode); reranks via `LLMRerank` or custom `CohereRerank`-style wrapper calling :8765 sidecar; returns `list[Hit]` (same type as current `retrieval.py` — drop-in) |
| `aiforge_core/rag/ingestion.py` | `ingest_chunk(tier, wing, text, metadata)` — `SimpleNodeParser` → `VectorStoreIndex.insert_nodes`; replaces direct `store_v2.upsert_memory` for T4 reindex path |
| `tests/python/test_rag_retriever.py` | Unit tests against a local `memories` fixture (pgvector, no sidecar required via mock); verify RRF fuse order, role policy tier application, graceful sidecar-down fallback |

#### Existing files to modify

| File | Change |
|------|--------|
| `aiforge_core/runtime/memory.py:Memory.search` | Check `get_flag("rag.backend", "legacy")`. If `"llamaindex"`: call `retrieve_for_role_li(...)` instead of `retrieve_for_role(...)`. Return type is the same `list[SearchResult]`. |
| `aiforge_core/runtime/memory.py:Memory.retain_fact` | No change in Phase 2. LlamaIndex ingestion path (`ingestion.py`) is wired only for T4 bulk reindex (Phase 2 bonus). T1/T2/T3 retain keeps `store_v2` path. |
| `scripts/runtime/reindex-daily.py` | Optionally: if `rag.backend==llamaindex`, call `ingest_chunk` per chunk instead of `store_v2.upsert_memory`. Gated by same flag. |
| `pyproject.toml` | Add LlamaIndex deps (see §5) |

#### Tests to add

- `tests/python/test_rag_retriever.py::test_role_policy_applies_correct_tiers`
- `tests/python/test_rag_retriever.py::test_rrf_fuse_order`
- `tests/python/test_rag_retriever.py::test_sidecar_down_fallback`
- `tests/python/test_rag_retriever.py::test_returns_same_hit_type_as_legacy`

#### Feature flag state at end of Phase 2

```
doer.backend = smolagents    # Phase 1 promoted to on
rag.backend  = legacy        # default until validation complete
orchestrator.backend = legacy
```

#### Rollback

Set `AIFORGE_FLAG_RAG_BACKEND=legacy`. `retrieve_for_role` in `retrieval.py` is never deleted — it becomes the fallback branch. LlamaIndex index does not mutate `memories` table (read-only retrieval path), so no data risk.

---

### Phase 3 — Orchestrator swap to LangGraph

**Goal:** Replace the launchd-tick-per-role pattern + `orchestrator.py` tool loop with a LangGraph `StateGraph`. Postgres remains the ticket store (LangGraph Postgres checkpointer wraps it). HITL nodes added at natural handoff points. Each existing role becomes a LangGraph node calling the same prompt + tool logic.

#### New files to create

| Path | Purpose |
|------|---------|
| `aiforge_core/graph/__init__.py` | package marker |
| `aiforge_core/graph/state.py` | `AgentState` TypedDict (see §2.1) |
| `aiforge_core/graph/edges.py` | `route_from_supervisor`, `after_doer`, `after_feedback` conditional functions |
| `aiforge_core/graph/graph.py` | `build_graph() → CompiledGraph`; wires all nodes + edges; attaches `PostgresSaver` checkpointer using `AIFORGE_DSN` |
| `aiforge_core/graph/nodes/supervisor.py` | Thin wrapper: load ticket from DB, call `_run_tool_loop(role_cfg["supervisor"], ...)`, write events, return updated `AgentState` |
| `aiforge_core/graph/nodes/planner.py` | Same pattern, role=planner |
| `aiforge_core/graph/nodes/doer.py` | Checks `flags["doer.backend"]`; routes to smolagents agent (Phase 1) or legacy tool loop |
| `aiforge_core/graph/nodes/feedback.py` | Same pattern, role=feedback; reads `verdict` from tool results, writes to `state["verdict"]` |
| `aiforge_core/graph/nodes/learner.py` | Same pattern, role=learner; calls `_write_t1_memory` |
| `aiforge_core/graph/nodes/retriever.py` | Standalone retriever node; called by planner/doer nodes before their tool loop if `flags["rag.backend"]=="llamaindex"`; injects results into context bundle |
| `aiforge_core/runtime/graph_runner.py` | `run_graph(ticket_id)` — entry point called by new `__main__` path; loads ticket, constructs initial `AgentState`, calls `graph.invoke(state, config={"thread_id": ticket.identifier})` |
| `scripts/runtime/com.aiforge.graph-runner.plist` | New launchd plist: polls for `todo` tickets (any role) every 60 s, calls `python -m aiforge_core.runtime.graph_runner` |
| `tests/python/test_graph_edges.py` | Unit tests for all conditional edge functions against mock `AgentState` dicts |
| `tests/python/test_graph_integration.py` | Integration test: full Supervisor→Planner→Doer→Feedback→Learner run against a local Postgres test DB (tickets + memories fixtures) with mocked LLM calls |

#### Existing files to modify

| File | Change |
|------|--------|
| `aiforge_core/runtime/__main__.py` | Check `get_flag("orchestrator.backend", "legacy")`. If `"langgraph"`: call `graph_runner.run_graph(ticket_id_from_claim_next())`. If `"legacy"`: call existing `tick(role)`. |
| `aiforge_core/runtime/orchestrator.py` | No deletions. Extract `_run_tool_loop` signature so graph nodes can call it directly. Add `__all__` export list so graph nodes import cleanly. |
| `aiforge_core/runtime/tickets.py` | Add `claim_next_any() → Ticket | None` — claims oldest `todo` across all roles; needed by the single graph-runner plist (vs 5 per-role plists). Existing `claim_next(role)` unchanged for legacy path. |
| `db/migrations/` | Add `2026-04-23-langgraph-checkpoints.sql` — creates `checkpoints` + `checkpoint_blobs` tables required by `langgraph-checkpoint-postgres`. **Additive only, no existing table changes.** |
| `pyproject.toml` | Add LangGraph deps (see §5) |

#### Tests to add

- `tests/python/test_graph_edges.py::test_route_supervisor_to_planner`
- `tests/python/test_graph_edges.py::test_after_doer_escalates_on_two_compile_fails`
- `tests/python/test_graph_edges.py::test_after_feedback_pass_routes_to_learner`
- `tests/python/test_graph_edges.py::test_after_feedback_scope_violation_aborts`
- `tests/python/test_graph_integration.py::test_full_pipeline_happy_path` (mocked LLM)

#### Feature flag state at end of Phase 3

```
doer.backend         = smolagents   # stable from Phase 1
rag.backend          = llamaindex   # stable from Phase 2
orchestrator.backend = legacy       # default; flip to langgraph per-ticket-type
```

#### Rollback

Set `AIFORGE_FLAG_ORCHESTRATOR_BACKEND=legacy`. The 5 per-role launchd plists are never removed during Phase 3 — they remain bootloaded, just idle if `orchestrator.backend=langgraph` is set. Flip the flag back and they resume normal operation immediately. LangGraph checkpoint tables in Postgres are additive; removing them requires only `DROP TABLE checkpoints, checkpoint_blobs` — no effect on `tickets` or `memories`.

---

## 4. Top-5 risks and mitigations

| # | Risk | Mitigation |
|---|------|------------|
| 1 | **smolagents `EditBlockTool` fails on Java strings with embedded quotes/backslashes** (confirmed in eval tc-1) | Use `find`/`replace` as separate string args (not embedded JSON); validate round-trip before writing; `num_retries=1` gives one re-attempt before escalating |
| 2 | **LlamaIndex `LlamaIndexPGVectorStore` schema mismatch** — LlamaIndex expects its own node table, not our `memories` table | Use `PGVectorStore` in custom mode with explicit `text_search_config` and column map; alternatively keep LlamaIndex as a retrieval-only layer that queries `memories` via raw SQL adapters wrapped in `BaseRetriever` |
| 3 | **LangGraph Postgres checkpointer writes large JSONB blobs per step** — 50-turn Doer tick = 50 checkpoint rows; may bloat DB | Set `checkpoint_during=False` for Doer node (only checkpoint at node boundaries, not each LLM turn); add `VACUUM ANALYZE checkpoints` to maintenance cron |
| 4 | **Per-role fcntl lock (`/tmp/aiforge-tick-<role>.lock`) has no equivalent in LangGraph graph runner** — two graph runner instances could double-claim | `claim_next_any()` uses `SELECT FOR UPDATE SKIP LOCKED` (already in `tickets.py`); graph runner needs same per-ticket thread_id lock via `PostgresSaver` thread isolation; add explicit `claim_next_any_with_lock()` that holds the Postgres advisory lock for ticket duration |
| 5 | **`memguard.ensure_loaded` is called inside `orchestrator.py:tick()` before the tool loop** — LangGraph nodes each call the relevant role node independently, breaking the single-entry memguard call | Each graph node must call `memguard.ensure_loaded(role_cfg.model, ...)` at node entry; extract the memguard call from `tick()` into a shared `pre_node_hook(role)` called by all nodes |

---

## 5. Dependency additions to `pyproject.toml`

```toml
[project]
dependencies = [
  # existing
  "psycopg[binary]>=3.2",
  "fastapi>=0.115",
  "uvicorn[standard]>=0.32",
  "pydantic>=2.8",
  "httpx>=0.27",
  "openai>=1.50",
  "numpy>=1.26",

  # Phase 1 — smolagents Doer
  "smolagents>=1.13",
  "litellm>=1.40",              # smolagents uses litellm as its LLM backend shim

  # Phase 2 — LlamaIndex RAG
  "llama-index-core>=0.12.0",
  "llama-index-vector-stores-postgres>=0.3.0",
  "llama-index-embeddings-openai>=0.3.0",    # used with base_url override → bge-m3 sidecar
  "llama-index-retrievers-bm25>=0.5.0",

  # Phase 3 — LangGraph orchestrator
  "langgraph>=0.3.0",
  "langgraph-checkpoint-postgres>=2.0.0",
]
```

**Version rationale:**
- `smolagents>=1.13` — minimum version that ships `ToolCallingAgent` with stable `edit_block`-compatible tool protocol; eval used 1.13.x
- `litellm>=1.40` — smolagents hard-depends on litellm; 1.40 supports LM Studio's OpenAI-compat endpoint without patching
- `llama-index-core>=0.12.0` — 0.12.x is the stable post-refactor branch with `VectorStoreIndex` + `QueryFusionRetriever` (RRF mode); avoids 0.10.x API
- `llama-index-vector-stores-postgres>=0.3.0` — pgvector support; `0.3.x` drops legacy `asyncpg` requirement, works with existing `psycopg` pool
- `llama-index-embeddings-openai>=0.3.0` — supports `api_base` override so we point it at :8764 bge-m3 sidecar
- `llama-index-retrievers-bm25>=0.5.0` — `BM25Retriever` compatible with `QueryFusionRetriever` node
- `langgraph>=0.3.0` — 0.3.x has stable `StateGraph` + `TypedDict` state + `PostgresSaver`; 0.2.x had breaking checkpointer API changes
- `langgraph-checkpoint-postgres>=2.0.0` — matches `langgraph>=0.3.x` checkpoint protocol; uses `psycopg3` (our existing driver)

---

## 6. Migration sequence summary

```
Phase 1  [Doer only]          ~1 week  doer.backend=smolagents
Phase 2  [RAG only]           ~1 week  rag.backend=llamaindex
Phase 3  [Orchestrator]       ~2 weeks orchestrator.backend=langgraph

Each phase: write tests → implement → flag-off validation → flag-on canary → promote
```

All three phases can overlap in development branches. They must be sequenced in production promotion: Phase 1 stable before Phase 2 canary; Phase 2 stable before Phase 3 canary.

---

## 7. What is explicitly NOT changing

- `aiforge_core/runtime/roles.py` — all five system prompts verbatim
- `aiforge_core/runtime/tickets.py` — Postgres ticket CRUD
- `aiforge_core/runtime/memguard.py` — RAM guard logic
- `aiforge_core/runtime/api.py` — FastAPI + React UI
- `aiforge_core/runtime/logging_setup.py` — structured NDJSON log format
- `db/migrations/2026-04-21-tickets.sql` — tickets schema (no column changes)
- `aiforge_core/store_v2.py` — retain_fact write path (until Phase 2 optional bonus)
- `aiforge_core/embed.py` — bge-m3 sidecar client (LlamaIndex uses same endpoint)
- `scripts/runtime/install.sh` — provisioning (add pip installs only)
- `web/` — React UI entirely untouched
