# AIForgeCrew

Autonomous code-fix pipeline. Plain-language ticket → enriched intent → PR.

```
human text ─► IntentLayer ─► EnrichedTicket ─► Planner ──► Doer ⇄ Feedback ─► Publish ─► Learner
                  │                                ▲                            │           │
                  └─────────► UnifiedContext ◄─────┴────────────────────────────┘           │
                                  (8 sources, ONE read API for all agents)                  │
                                                                                            ▼
                                                                                    T2/T3 facts → memory
```

Two architectural rules:

1. **One ingress** — every plain-language input (chat OR ticket POST) is translated by `IntentLayer` into a structured `Intent` + `EnrichedTicket` before any agent sees it.
2. **One read API** — every agent (chat / planner / doer) gets its context from `UnifiedContext`, never from a backend directly. Memory layers, code index, learner facts, repo doc, operator notes — all behind one call.

---

## Topology

```
laptop ──ssh──► nuc :8799 (api)
                  │
                  ├── postgres 16     ── tickets, ticket_events, costs, hitl_requests
                  ├── neo4j 5.26      ── 5-tier memory · :Repo standards · :Symbol/:Chunk index
                  ├── graph-runner    ── ADK 1.31.1 SequentialAgent[Planner, Loop[Doer,Feedback], Learner]
                  ├── embed-sidecar   :8764 bge-m3 ONNX  (writes T4/T5 vectors)
                  ├── graphify        ── tree-sitter ingest → :Symbol + INFERRED edges (T5)
                  ├── aider-repomap   ── PageRank tag digest over T5 + on-disk source (T4)
                  └── ops MCPs        :8810 mongo · :8811 k8s · :8812 tekton · :8813 tally

mac studio :1234 ◄── llm ── doer    (qwen3-coder-next, mlx-lm)
mac studio :1235 ◄── llm ── planner (qwen3.6-27B 4bit, mlx-lm)  ── also serves IntentLayer
ollama cloud  ◄── llm ── chat       (qwen3-coder-next default)
```

---

## IntentLayer — plain text → structured Intent

`aiforge_core/intent/classifier.py`

```
raw_text ─► classify() ── Qwen 3.6 27B JSON-strict ─► Intent{
                                                       action,        # add|edit|fix|remove|dup|investigate|refactor|test|doc|ops
                                                       entity,        # noun being acted on
                                                       reference_pattern,  # exemplar to mirror
                                                       repo_hint,
                                                       keywords,
                                                     }
                                              │
                                              └── (LLM down) ─► regex heuristic fallback

Intent ─► enrich() ─► UnifiedContext ─► EnrichedTicket{
                                          intent,
                                          allowed_files, reference_files,
                                          similar_tickets, t3_recipes,
                                          commands, acceptance, repo,
                                          sources_used, errors,
                                        }
```

Wired at:

| Site | Effect |
|---|---|
| `POST /api/tickets` | `payload.body` is enriched before insert; result attached as `metadata.enrichment`. Disable: `AIFORGE_INTENT_ENRICH=0` |
| chat handler `do_unified_memory_query` | tool returns `UnifiedContext.for_chat(query).render()` |
| Planner `runner.py` | `context_bundle = UnifiedContext.for_planner(ticket).render()` |
| Doer `_build_user_input` | `unified_section = UnifiedContext.for_doer(ticket).render()` prepended to prompt |

LLM call is local-only (`AIFORGE_INTENT_LM_URL` defaults to planner port). No cloud.

---

## UnifiedContext — one read API, 8 sources

`aiforge_core/context/unified.py`

| # | Source | What | Module |
|---|---|---|---|
| 1 | `unified_query` | T2 facts + T1 episodic + ticket brief + related symbols + doc/markdown + external lib docs (6-source aggregator) | `aiforge_core/memory/unified_query.py` |
| 2 | Aider RepoMap | tree-sitter PageRank tag digest over T4/T5 — keyed off focal_files extracted from (1) | `aiforge_core/memory/code_context.py` + `aiforge_core/index/aider_map.py` |
| 3 | graph_neighbours | Neo4j `:Symbol` CALLS / IMPORTS / EXTENDS edges (T5) | `aiforge_core/memory/code_context.py` |
| 4 | repo_standards | per-repo `:Repo` manifest — build/compile/test/lint/format/conventions/forbidden_patterns/acceptance | `aiforge_core/runtime/repo_standards.py` |
| 5 | similar_tickets | Postgres `tickets` ILIKE on title+body | inline |
| 6 | T3 patterns | learner-written + auto-promoted recipes (`Memory.search` filtered to tier='t3') | `aiforge_core/runtime/memory.py` |
| 7 | repo_doc | top-of-worktree `CLAUDE.md` / `README.md` / `.aiforge/CONVENTIONS.md` tail | inline |
| 8 | claude_memory | grep over operator's `~/.claude/memory/*.md` | inline |

Each source is best-effort. Failures populate `bundle.errors`, never crash the caller. Token budget is per-bundle (default 4K).

```
UnifiedContext().for_intent(intent, role=, token_budget=)  ─► ContextBundle
                .for_chat(text)
                .for_planner(ticket)
                .for_doer(ticket)
```

`ContextBundle.render()` produces a single prompt-ready Markdown block.

---

## Memory model — Neo4j 5 tiers

```
T1  :Episode    ── per-stage events (write: api stage hooks)
T2  :Fact       ── canon facts, ground truth (write: learner / chat retain)
T3  :Pattern    ── recipes, learned skills (write: learner + pattern_miner; read: UnifiedContext)
T4  :Chunk      ── code chunks, markdown ingests (write: graphify; read: aider digest)
T5  :Symbol     ── tree-sitter symbols + Graphify INFERRED edges (write: treesitter_ingest + graphify; read: aider + graph_neighbours)
```

Lifecycle (deterministic templates, no LLM):

```
write ─► retain_fact ─► neo4j ─► search hits++
                              │
                              ├── decay         (>90d, hit_count=0 → archived)
                              └── pattern_miner (3+ similar outcomes → T3 auto-promoted)
```

The learner runs after every Doer pass (`aiforge_core/runtime/doer_learner.py`) — distills outcome to one T3 fact. The pattern miner (`aiforge_core/memory/pattern_miner.py`) promotes 3+ similar outcomes to a single T3 recipe. Both **read paths** for those facts are now closed via UnifiedContext source #6.

---

## Agents

| Agent | Runtime | Model | Inputs (now) | Tools (allowlist) | Memory R/W |
|---|---|---|---|---|---|
| **chat** | GA loop (`_chat_via_ga`) | qwen3-coder-next via Ollama Cloud | UnifiedContext.for_chat(query) injected via `do_unified_memory_query` tool | search_memory, unified_memory_query, related_memories, find_doc, sym_lookup, ticket_brief, ops_* (mongo/k8s/tekton/tally), read_claude_memory | R full · W T3 (chat_qa wing, auto) |
| **planner** | smolagents CodeAgent | Qwen 3.6 27B (mlx-lm :1235) | UnifiedContext.for_planner(ticket) → `task_prompt` | read_file, list_dir, grep_repos, write_plan, related_tickets, related_memories | R full · W ticket body |
| **doer** | GA agent_runner_loop | qwen3-coder-next (mlx-lm :1234) | UnifiedContext.for_doer(ticket) prepended to prompt | file_read, file_patch, file_write, code_run, glob, grep, edit_block, plan_mode, todos, subagent, hooks, sandbox, secrets, ... (24 ga_tools) | R full · W via learner (T3) |
| **feedback** | deterministic Python | (none) | doer outcome counters | (none — pure code) | R none · W ticket_events |
| **learner** | deterministic + optional LLM | distill = template; pattern_miner = heuristic | Doer outcome dict | retain_fact | W T3 (patterns/doer-success or patterns/doer-failure) |

ADK 1.31.1 `SequentialAgent[Planner, LoopAgent[Doer, Feedback], Learner]` orchestrates. ADK does scheduling + lifecycle + tool-allowlist enforcement only — no business logic.

---

## Self-learning loop (now closed)

```
ticket ─► doer ──► outcome ─► doer_learner.distill() ─► T3 fact
                                                          │
                                            pattern_miner.run() (cron)
                                                          │
                                            ≥3 similar    ▼
                                            ──────► T3 auto-promoted
                                                          │
   next ticket ─► IntentLayer ─► UnifiedContext ─► T3 recipes block ──► doer prompt
```

Source #6 in UnifiedContext (`_t3_recipes`) closes the read end. Disable write: `AIFORGE_DOER_AUTOLEARN=0`. Promote: `aiforge memory mine`.

---

## Security rails

| Surface | Control |
|---|---|
| `code_run` shell | `firejail` / `docker` sandbox · `AIFORGE_DOER_SANDBOX=firejail\|docker` |
| Secrets in commits | gitleaks / trufflehog / regex chain · builtin `pre_commit` hook · `block:true` |
| Doer scope | `ScopeGuard` against `allowed_files` allowlist (single chokepoint `_get_abs_path`) |
| Forbidden tools | `tool_before_callback` deny-list per role (defined in `agents.yaml`) |
| Plan Mode | Read-only think before writes unlock |
| Ops MCPs | QA tier default · `ALLOW_PROD` env gate |
| Tool args | `code_run` rejects shell discovery (grep/find/ls/cat); `mvn` capped at 2/ticket |
| Memory access | Role policy (sr_developer/sr_arch/qa) per-tier wing filter |
| Credentials | All keys via env (`runtime.env`); never in source/commits |

---

## Endpoints

| Path | What |
|---|---|
| `POST /api/chat/ask` | GA-backed chat, UnifiedContext-backed, auto-retain |
| `POST /api/tickets` | Submit work — enriched via IntentLayer before insert |
| `GET  /api/tickets/{id}` | Detail incl. `metadata.enrichment` |
| `GET  /api/agents` | Per-role config from `agents.yaml` |
| `GET  /api/runtime/llm_backend` | Active provider + registry |
| `GET  /api/runtime/cost?ticket=X` | $/Mtok rollup |
| `GET  /api/runtime/perf` | Aggregated step waterfall (`hooks.emit_step`) |
| `GET  /api/repo/standards?name=X` | Per-project manifest |
| `GET  /api/workflow/topology?ticket=X` | DAG snapshot |
| `GET  /api/workflow/stream?ticket=X` | SSE live overlay |
| `PUT  /api/config/agents/{role}` | Swap provider/model |

---

## Bring-up

```
# NUC
cd ~/AIForgeCrew && uv sync
systemctl --user start aiforge-api               # :8799
systemctl --user start aiforge-graph-runner
systemctl --user start aiforge-embed-sidecar     # :8764

# Mac Studio (LLMs)
mlx_lm.server --model qwen3-coder-next --port 1234
mlx_lm.server --model qwen3.6-27b --port 1235

# UI
cd web && npm install && npm run build           # served at :8799/ui
```

---

## Env knobs

```
# Models / providers
AIFORGE_PRIMARY_BACKEND        local|anthropic|openai|ollama_cloud
AIFORGE_<ROLE>_PROVIDER        per-role override
AIFORGE_<ROLE>_MODEL           per-role model id
AIFORGE_PLANNER_MODEL          default qwen3.6-27b
AIFORGE_INTENT_LM_URL          default = planner LM URL (port 1235)
AIFORGE_INTENT_MODEL           default = AIFORGE_PLANNER_MODEL

# IntentLayer / UnifiedContext
AIFORGE_INTENT_ENRICH          1 = enrich on ticket POST (default 1)
AIFORGE_CLAUDE_MEMORY_DIR      operator memory grep root (default ~/.claude/memory)
AIFORGE_AIDER_REPOMAP_TOKENS   default 1024
AIFORGE_DOER_NEIGHBOURS_LIMIT  default 30

# Doer behaviour
AIFORGE_DOER_PLAN_MODE         1 = think-before-edit (default)
AIFORGE_DOER_TODOS             1 = in-loop checklist
AIFORGE_DOER_SUBAGENT          1 = isolated sub-agent dispatch
AIFORGE_DOER_HOOKS             1 = .aiforge/hooks.yml + secret-scan
AIFORGE_DOER_COMPACT           1 = middle-out history elision
AIFORGE_DOER_SANDBOX           firejail|docker
AIFORGE_DOER_AUTOLEARN         1 = distill outcome to T3 (default)

# Chat
AIFORGE_CHAT_AUTORETAIN        1 = auto-write Q+A to T3 (default)

# Infra
AIFORGE_NEO4J_URI              bolt://127.0.0.1:7687
AIFORGE_DSN                    postgres dsn
AIFORGE_EMBED_URL              http://127.0.0.1:8764
AIFORGE_OTEL_ENABLED           1 = OpenTelemetry export
AIFORGE_PERF_NDJSON            1 = ~/.aiforge/perf.ndjson tail (default)
```

---

## Source layout

```
aiforge_core/
├── intent/             ─── IntentLayer: plain text → Intent → EnrichedTicket
│   └── classifier.py
├── context/            ─── UnifiedContext: one read API, 8 sources
│   └── unified.py
├── runtime/            ─── api · adk_workflow · cost · otel · memory · repo_standards
├── doer/
│   ├── ga_runner.py    ─── GA agent loop bridge (prompt assembly)
│   └── ga_tools/       ─── 24 modules, one per tool concern
├── planner/            ─── smolagents CodeAgent
├── memory/
│   ├── unified_query.py    ─── 6-source aggregator (used by UnifiedContext source #1)
│   ├── code_context.py     ─── aider_digest + graph_neighbours (sources #2, #3)
│   ├── pattern_miner.py    ─── 3+ similar → T3 auto-promote
│   └── decay.py            ─── archive stale facts
├── index/              ─── aider_map · merkle · symbol_embed · docs_index
├── llm/                ─── client · router · cache_markers · rate_limiter · providers
└── agents.yaml         ─── single source of truth for per-agent contracts
```

Single doc — this file. `agents.yaml` is the only other prose source (per-agent allowed/forbidden tools, memory scopes, termination contracts).
