# AIForgeCrew

Autonomous code-fix pipeline. Ticket in → PR out.

```
ticket ──► Planner ──► Doer ⇄ Feedback ──► Integration ──► Publish ──► Learner
                                                                         │
                                                                         ▼
                                                                      memory
```

## Features

| Layer | Wins |
|---|---|
| **Chat** | GA loop · ops MCPs (mongo/k8s/tekton/tally) · unified memory query · auto-learn Q+A |
| **Doer** | Plan Mode · TodoWrite · Sub-agents · Hooks · Auto-compaction · Sandbox · Secret-scan |
| **Memory** | Neo4j 5-tier · Aider RepoMap · mtime cache · auto-distill · decay · pattern-mining |
| **LLM** | Local mlx-lm · Anthropic · OpenAI · Ollama Cloud · per-role + global routing · rate-limiter · prompt-cache |
| **Tools** | 25 ga_tools · 102 ops MCPs/tier · 22 graph_rag MCP · external docs index |
| **Obs** | OpenTelemetry · cost $/turn · per-ticket rollup · DAG topology view |

## Topology

```
laptop ──ssh──► nuc :8799 (api)
                  │
                  ├── postgres 16    (tickets, events, costs)
                  ├── neo4j 5.26     (memory, repos, symbols)
                  ├── graph-runner   (ADK 1.31.1 sequential)
                  └── ops MCPs       :8810 mongo · :8811 k8s · :8812 tekton · :8813 tally
                          │
mac studio :1234 ◄───llm──┤ doer (mlx-lm)
mac studio :1235 ◄───llm──┤ planner (mlx-lm)
ollama cloud ◄──────llm───┘ chat (qwen3-coder-next default)
```

## Memory

```
T1 episodic ── ticket events
T2 canon    ── repo facts, ground truth
T3 patterns ── chat Q+A, doer outcomes, auto-promoted
T4 code     ── markdown ingests, source snippets
T5 symbols  ── :Symbol embeddings (vector NN)

         ┌──── unified_memory_query (one tool, 6 sources)
chat  ──►├──── search_memory  (hybrid)
         ├──── ticket_brief
         ├──── related_memories
         ├──── sym_lookup / find_similar_code
         ├──── find_doc
         └──── docs_index (external libs)
```

Lifecycle:

```
write ──► retain_fact ──► neo4j  ──► search hits++
                                  │
                                  ├── decay   (>90d, hit_count=0 → archived)
                                  └── miner   (3+ similar → T3 pattern)
```

## Security audit

| Surface | Control |
|---|---|
| `code_run` shell | `firejail` / `docker` sandbox · `AIFORGE_DOER_SANDBOX=firejail\|docker` |
| Secrets in commits | `gitleaks` / `trufflehog` / regex heuristic · builtin `pre_commit` hook · `block:true` |
| Doer scope | `ScopeGuard` against allowed-files allowlist (`_get_abs_path` chokepoint) |
| Forbidden tools | `tool_before_callback` deny-list per role |
| Plan Mode | Read-only think before writes unlock |
| Ops MCPs | QA tier default · `ALLOW_PROD` env gate |
| Tool args | `code_run` rejects shell discovery (`grep`/`find`/`ls`/`cat`); `mvn` capped at 2/ticket |
| Path traversal | All file reads via `_get_abs_path` resolved against worktree |
| Memory access | Role policy (sr_developer/sr_arch/qa) per-tier wing filter |
| LLM secrets | API keys via `runtime.env` only · never in code/commits |
| Neo4j auth | bolt password from env · localhost-only port |
| Postgres | `DSN` env, no embedded creds in source |

Rails:

```
ticket ─► [scope_guard] ─► [plan_mode] ─► [tool_before_callback] ─► do_<tool>
                                                                       │
                            [hooks pre_commit] ◄── secret_scan ◄───────┘
```

## Design — KISS

```
aiforge_core/
├── runtime/        ─── orchestrator, api, otel, cost, mentions, hitl
│   ├── api.py      ─── FastAPI routes
│   ├── otel.py     ─── one entry, no-op when disabled
│   ├── cost.py     ─── $/Mtok rate table + postgres rollup
│   ├── memory.py   ─── neo4j adapter
│   ├── repo_standards.py ─── per-project build/test/lint manifest
│   ├── workflow_topology.py ─── DAG snapshot for UI
│   └── adk_workflow.py ─── ADK 1.31.1 SequentialAgent
│
├── doer/
│   ├── ga_runner.py ── GA agent_runner_loop bridge
│   └── ga_tools/    ── one file per concern (24 modules):
│        plan_mode · todos · subagent · hooks · compaction · sandbox
│        secrets · bash · batch · bulk_edit · edit_verify · glob · grep
│        java_refactor · lint · tests · undo · web_search · ...
│
├── memory/
│   ├── unified_query.py  ── one tool, 6 sources, ranked
│   ├── decay.py          ── archive stale facts
│   └── pattern_miner.py  ── 3+ similar → T3 pattern
│
├── index/
│   ├── aider_map.py    ── tree-sitter + PageRank, mtime cache
│   ├── merkle.py       ── file→folder→root sha256, persistent
│   ├── symbol_embed.py ── :Symbol vector NN
│   └── docs_index.py   ── external lib chunks (sqlite + bge-m3)
│
├── llm/
│   ├── client.py        ── one complete() entry
│   ├── router.py        ── role → provider, fallback chain
│   ├── cache_markers.py ── anthropic ephemeral / openai prefix / gemini stub
│   ├── rate_limiter.py  ── per-provider token bucket
│   └── providers/       ── local · anthropic · openai · gemini (hidden) · ollama_cloud
│
└── planner/         ── smolagents CodeAgent (EVAL-1 winner)
    └── ga_runner.py ── parallel GA-based planner (AIFORGE_PLANNER_BACKEND=genericagent)
```

Rules followed everywhere:

- one file per concern
- exports `SCHEMA` + `handle()` for every tool
- env-flag gated; default-off until smoke
- thin wrapper in `do_<tool>`, pure logic in module
- best-effort persist; soft fail; never crash agent loop
- no LLM call in distillers / decay / miner — deterministic templates

## Workflow viz

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

Solid edge = forward · dotted edge = feedback loop. Live view at `/ui/workflow?ticket=ONE-99`.

## Endpoints

| Path | What |
|---|---|
| `POST /api/chat/ask` | GA-backed chat, auto-retain |
| `POST /api/tickets` | Submit work |
| `GET  /api/agents` | Per-role config |
| `GET  /api/runtime/llm_backend` | Active provider + registry |
| `GET  /api/runtime/cost?ticket=X` | $/Mtok rollup |
| `GET  /api/repo/standards?name=X` | Per-project manifest |
| `GET  /api/workflow/topology?ticket=X` | DAG snapshot |
| `PUT  /api/config/agents/{role}` | Swap provider/model |

## Bring-up

```
# NUC
cd ~/AIForgeCrew && uv sync
systemctl --user start aiforge-api    # :8799
systemctl --user start aiforge-graph-runner

# UI
cd web && npm install && npm run build  # served at :8799/ui
```

## Env knobs

```
AIFORGE_PRIMARY_BACKEND        local|anthropic|openai|ollama_cloud
AIFORGE_<ROLE>_PROVIDER        per-role override
AIFORGE_<ROLE>_MODEL           per-role model id
AIFORGE_DOER_PLAN_MODE         1 = think-before-edit
AIFORGE_DOER_TODOS             1 = in-loop checklist
AIFORGE_DOER_SUBAGENT          1 = isolated sub-agent dispatch
AIFORGE_DOER_HOOKS             1 = .aiforge/hooks.yml + secret-scan
AIFORGE_DOER_COMPACT           1 = middle-out history elision
AIFORGE_DOER_SANDBOX           firejail|docker
AIFORGE_DOER_OPS_MCP           1 = ops MCPs in doer
AIFORGE_CHAT_OPS_MCP           1 = ops MCPs in chat (default)
AIFORGE_AIDER_MAP_CACHE        1 = mtime-keyed RepoMap cache (default)
AIFORGE_OTEL_ENABLED           1 = OpenTelemetry export
AIFORGE_PROMPT_CACHE           1 = provider cache hints (default)
AIFORGE_MEMORY_BACKEND         postgres|neo4j (default neo4j)
```

## Status

```
phase A    chat replatform + GA migration       SHIPPED
phase A    Aider/CC gap modules (8)             SHIPPED
phase A    ops MCPs in chat + doer              SHIPPED
phase A    unified memory query                 SHIPPED
phase A    repo_standards Neo4j catalogue       SHIPPED
phase A    otel + cost + cache + sandbox + sec  SHIPPED (env-gated)
phase A    auto-learn + decay + pattern-mining  SHIPPED
phase A    @-mentions + external docs index     SHIPPED
phase A    code-chunk embeddings + Merkle tree  SHIPPED
phase A    workflow viz UI                      SHIPPED
phase B    ADK 2.0.0b1 sidecar                  SKELETON (scripts/runtime/adk2-sidecar)
phase B    HITL request_input resume            STUB → ADK 2.0
phase B    smolagents drop                      KEEP for Planner (EVAL-1 winner)
```
