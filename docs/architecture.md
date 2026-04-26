# Architecture

## End-to-end flow

```
ticket ─► Architect ─► Planner ─► Doer ⇄ Feedback ─► Integration ─► Publish ─► Learner
                                                                                  │
                                                                                  ▼
                                                                          Neo4j memory (T1..T5)
```

## Hosts

```
laptop  ─ssh─►  nuc :8799 (api) ──┬── postgres 16  (tickets, events, costs, hitl_pending)
                                  ├── neo4j 5.26   (memory, repos, symbols, embeddings)
                                  ├── graph-runner (ADK 1.31.1 sequential)
                                  ├── embed sidecar :8764 (bge-m3 ONNX)
                                  └── ops MCPs    :8810 mongo · :8811 k8s · :8812 tekton · :8813 tally

mac studio :1234  ◄─ llm ──  doer    (mlx-lm Qwen3-Coder-Next)
mac studio :1235  ◄─ llm ──  planner (mlx-lm Qwen3.6-27B)
ollama cloud      ◄─ llm ──  chat    (qwen3-coder-next default)
```

## Agents (5 roles + chat)

| Role | Backend | Model | Max turns | Wall cap | Tools |
|---|---|---|---|---|---|
| **Architect** | Claude Code (laptop, ext) | n/a | n/a | n/a | writes tickets only |
| **Planner** | smolagents CodeAgent (or GA via flag) | Qwen3.6-27B :1235 | 12 | 8 min | read_file, ask_explorer, lookup_repo, write_plan |
| **Doer** | GA text-protocol | qwen-coder-next :1234 | 40 | 25 min | 25 ga_tools — see [tools.md](tools.md) |
| **Feedback** | LLM | shared | 6 | 2 min | targeted_fixlist, retry verdict |
| **Learner** | LLM | shared | 4 | 2 min | distill, retain_fact |
| **Chat** | GA + ollama_cloud | qwen3-coder-next | 12 | 1 min | unified_memory_query · ops MCPs · graph_rag MCP · final_answer |

## Workflow graph

```
            ┌────────┐    ┌────────┐    ┌──────────┐
START ─────►│Planner │───►│  Doer  │───►│ Feedback │
            └────────┘    └────┬───┘    └────┬─────┘
                               ▲             │
                               └─loop (compile_red)─┘
                                             │ (compile_green)
                                             ▼
                                    ┌─────────────┐    ┌──────────┐
                                    │ Integration │───►│ Publish  │
                                    └─────────────┘    └────┬─────┘
                                                            ▼
                                                       ┌─────────┐
                                                       │ Learner │ ─► T3 fact
                                                       └─────────┘
```

Live SSE view: `/ui/workflow?ticket=ONE-99`. Solid green = forward · dotted grey = loop.

## Data flow

```
operator ─► POST /api/tickets ─► postgres `tickets`
                                     │
                                     ▼
                                graph-runner picks
                                     │
                ┌────────────────────┼────────────────────┐
                ▼                    ▼                    ▼
            Planner               Doer               Feedback
            ▼                    ▼                    ▼
        plan.md          file_patch+verify       targeted_fixlist
            │                    │                    │
            └─► neo4j ◄──────────┴── ndjson ──────────┘
                              ↓
                         hooks emit_step
                              ↓
                     ~/.aiforge/perf.ndjson
                              ↓
                     /api/runtime/perf
```

## Layered stores

```
postgres ── tickets · events · llm_costs · hitl_pending
neo4j   ── :Memory (T1..T4) · :Symbol+embedding (T5) · :Repo standards · :File · :Fact
sqlite  ── ~/.aiforge/merkle/<repo>.db (file→folder→root sha256 tree)
sqlite  ── ~/.aiforge/docs/<lib>.db    (external library chunks + bge-m3 vectors)
ndjson  ── ~/.aiforge/perf.ndjson      (per-step wall_ms)
```

## Hot path — chat

```
HTTP POST /api/chat/ask
  │
  ├─ otel.span("chat.via_ga")
  │
  ├─ GA LLMSession (cache_markers wired)  ──►  ollama cloud
  │
  ├─ unified_memory_query  ──►  neo4j hybrid + ticket_brief + sym_lookup + find_doc + docs_index
  ├─ ops_<server>_<tool>   ──►  mongo/k8s/tekton/tally MCP
  │
  ├─ final_answer | fallback (last tool_result) | fallback (assistant text)
  │
  ├─ hooks.emit_step  ──►  ~/.aiforge/perf.ndjson + /api/runtime/perf
  ├─ cost.record_call ──►  postgres llm_costs
  └─ Memory.retain_fact (auto) ──►  neo4j T3 patterns/chat-auto
```

## Hot path — doer

```
ticket id
  │
  ├─ ScopeGuard(allowed_files)
  ├─ Aider RepoMap (mtime-cached)
  ├─ Graphify neighbours (Neo4j hop)
  ├─ repo_standards (Neo4j :Repo + worktree YAML → env + prompt block)
  │
  ├─ GA agent_runner_loop
  │    │
  │    ├─ tool_before_callback ─► plan_mode guard, deny-list, perf t0
  │    ├─ do_<tool>             ─► see tools.md
  │    ├─ tool_after_callback   ─► perf emit_step
  │    └─ post_edit/post_compile hooks ─► .aiforge/hooks.yml
  │
  ├─ doer_learner.distill ─► neo4j T3 patterns/doer-success|failure
  ├─ feedback verdict      ─► loop or escalate
  └─ integration.smoke + publish.gh_pr
```

## Code layout

See [tools.md](tools.md) for the full ga_tools catalogue · [hooks.md](hooks.md) for event taxonomy.

```
aiforge_core/
├── runtime/        ── api · otel · cost · mentions · hitl · workflow_topology
│                     repo_standards · doer_learner · maintenance_cli
├── doer/
│   ├── ga_runner.py       ── GA agent_runner_loop bridge
│   └── ga_tools/          ── 24 KISS modules (one concern per file)
├── memory/         ── unified_query · decay · pattern_miner · schema
├── index/          ── aider_map · merkle · symbol_embed · docs_index
├── llm/
│   ├── client.py    · router.py · rate_limiter.py · cache_markers.py
│   └── providers/   ── local · anthropic · openai · gemini (hidden) · ollama_cloud
└── planner/         ── smolagents CodeAgent (default) | GA fallback
```
