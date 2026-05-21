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
                  ├── graph-runner    ── ADK 2.0.0b1 SequentialAgent[Planner, Verifier, Researcher, Loop[Doer, Refiner, Feedback], Learner]
                  │                     (Triage runs upstream in orchestration; Architect is external — 9 archetypes total)
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
| 2 | Aider RepoMap | tree-sitter PageRank tag digest over T4/T5 — keyed off focal_files extracted from (1) | `aiforge_core/memory/code_context.py` + `aiforge_core/indexing/aider_map.py` |
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

## Orchestrator

| Path | Surface | Pipeline |
|---|---|---|
| `aiforge_core/runtime/adk_runner.py` | HTTP API + systemd `aiforge-graph-runner.service` | ADK `SequentialAgent[Planner, Verifier, LoopAgent[Doer, Feedback], Learner]` (Architect = external Claude Code) |

Infrastructure: pluggable LLM router with health probe + cloud auto-escalation (`aiforge_core/llm/`), recovery engine over the F-001..F-012 failure taxonomy (`aiforge_core/orchestrator/recovery_engine.py`), and per-ticket NDJSON traces at `~/.aiforge/runs/<ticket>.ndjson`.

## Recovery & Resilience

| Layer | Module | Triggers |
|---|---|---|
| Circuit breakers | `aiforge_core/orchestrator/circuit_breakers.py` | wall-clock per agent (doer 30min · others 10min · ticket 4hr); retries-per-step (5); token budget (2× expected); audit failures |
| Failure taxonomy | `aiforge_core/orchestrator/failure_taxonomy.py` | F-001 hallucinated import · F-002 hallucinated symbol · F-003 diff context mismatch · F-004/007/008/010 loop · F-005 unreachable plan step · F-006 plan depth · F-009 token budget · F-011 skill misapplication · F-012 memory contradiction. User-extensible at `~/.aiforge/failure_taxonomy.yaml` (F-013+) |
| Detectors | `aiforge_core/orchestrator/detectors.py` | Real ground-truth checks via Neo4j Symbol/Import graph + 3x-same-output loop hash + udiff context line hash |
| Recovery engine | `aiforge_core/orchestrator/recovery_engine.py` | Maps detector match → `Action` (BLOCK_AND_RETRY · REPLAN · REPLAN_SMALLER · SPLIT_TICKET · KGR_FALLBACK · DEMOTE_SKILL · QUARANTINE_MEMORY · ESCALATE_HUMAN). Repeat-escalation guard: same F-mode 3× → forced ESCALATE_HUMAN regardless of policy. |
| LLM client | `aiforge_core/llm/client.py` | 3-tier retry chain — pre-flight cloud escalation when `est_tokens > local_ctx × 0.8`; fallback provider on transport error or empty/garbage 200-OK; cloud quality escalation on second failure |
| Provider health | `aiforge_core/llm/health.py` | Cached `/v1/models` probe (30s TTL); router skips known-down providers automatically; envs: `AIFORGE_HEALTH_DISABLE`, `AIFORGE_HEALTH_TTL_S`, `AIFORGE_HEALTH_TIMEOUT_S` |
| Cloud auto-escalation | `aiforge_core/llm/router.py:escalate()` | Reasons: `context_overflow` (token estimate vs local ctx), `quality` (forced cloud after empty/garbage), `timeout`, `breaker_close`. Pinnable via `AIFORGE_<ROLE>_CLOUD_PROVIDER` |

## Agents

| Agent | Runtime | Model | Inputs (now) | Tools (allowlist) | Memory R/W |
|---|---|---|---|---|---|
| **chat** | GA loop (`_chat_via_ga`) | qwen3-coder-next via Ollama Cloud | UnifiedContext.for_chat(query) injected via `do_unified_memory_query` tool | search_memory, unified_memory_query, related_memories, find_doc, sym_lookup, ticket_brief, ops_* (mongo/k8s/tekton/tally), read_claude_memory | R full · W T3 (chat_qa wing, auto) |
| **planner** | smolagents CodeAgent | Qwen 3.6 27B (mlx-lm :1235) | UnifiedContext.for_planner(ticket) → `task_prompt` | read_file, list_dir, grep_repos, write_plan, related_tickets, related_memories | R full · W ticket body |
| **doer** | GA agent_runner_loop | qwen3-coder-next (mlx-lm :1234) | UnifiedContext.for_doer(ticket) prepended to prompt | **editor** (view/create/str_replace/insert/undo_edit), **bash** (tmux persistent session), **think**, **finish**, grep_repo, fetch_url, git_commit, memory_lookup, graphify_lookup, update_working_checkpoint | R full · W via learner (T3) |
| **feedback** | deterministic Python | (none) | doer outcome counters | (none — pure code) | R none · W ticket_events |
| **learner** | deterministic + optional LLM | distill = template; pattern_miner = heuristic | Doer outcome dict | retain_fact | W T3 (patterns/doer-success or patterns/doer-failure) |

ADK 2.0.0b1 `SequentialAgent[Planner, LoopAgent[Doer, Feedback], Learner]` orchestrates. ADK does scheduling + lifecycle + tool-allowlist enforcement only — no business logic.

### Doer tool surface (OpenHands-parity sub-project #1, 2026-05-21)

The Doer calls four canonical tools, declared in `aiforge_core/agents/agents.yaml`:

| Tool | Module | Notes |
|---|---|---|
| `editor(command, path, ...)` | `runtime/tools/editor.py` | OH-style multi-command: `view`, `create`, `str_replace`, `insert`, `undo_edit` (per-path snapshot ring depth 5). Sub-command allowlist via `editor_commands` field in agents.yaml. |
| `bash(command, restart, timeout)` | `runtime/tools/bash.py` | tmux-backed persistent session per ADK run; cwd / env / background jobs persist across calls. Falls back to stateless subprocess if tmux missing. |
| `think(thought)` | `runtime/tools/cognition.py` | No-op + `:Think` trace event. 4 KB cap. |
| `finish(summary, status)` | `runtime/tools/cognition.py` | Doer-only explicit termination signal; returns `terminate=True`. |

Non-Doer agents (Architect, Planner, Researcher) get **view-only** access via the `editor_commands: [view]` field. The legacy `file_read / file_write / file_patch / list_dir / run_shell / code_run` tools are kept one release as hallucinated-name escape hatches in `runtime/doer_tools.py` and will be removed in the next minor release. See:

- Spec: `docs/superpowers/specs/2026-05-21-tool-surface-upgrade-design.md`
- Roadmap: `docs/superpowers/specs/2026-05-21-openhands-parity-roadmap.md` (subs #2-#9)
- Plan:  `docs/superpowers/plans/2026-05-21-tool-surface-upgrade.md`

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

# Retrieval (rerank + synonyms)
AIFORGE_RERANK_URL             http://127.0.0.1:8765 (cross-encoder sidecar)
AIFORGE_RERANK_DISABLE         1 = skip rerank pass even if sidecar live
AIFORGE_REPOS_BASE             /home/mani/codeRepo (root for synonyms.yml lookup)

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

## Project layout

```
aiforge_core/
  agents/         6 v5 archetypes (architect, planner, verifier, doer,
                  feedback, learner) + base + registry + agents.yaml
  orchestrator/   agent_runner + recovery + circuit_breakers
                  + tool_registry — wraps archetypes as ADK LlmAgents
  api/            FastAPI app + MCP HTTP shim (port 8799)
  cli/            python -m aiforge_core.cli.main — ticket / trace commands
  tickets/        Postgres ticket lifecycle (claim_next_any, status, events)
  llm/            litellm client + cache + router
  memory/         decay + pattern_miner + unified_query + Neo4j RAG +
                  embed + store + retrieval
  indexing/       code symbol indexing (docs, repo learn, embeddings)
  config/         env vars + agents.yaml loader + roles
  observability/  structured NDJSON logging + OpenTelemetry + cost
  workflows/      workflow definitions
  parallel.py     thread-pool helpers
```

Entry points (per `pyproject.toml`):
- `aiforge-agents-run` — run one ticket through the orchestrator
- `aiforge-agents-tb` — trial-balance memory process

Services on the NUC (`192.168.70.115`):
- `aiforge-api.service` — uvicorn FastAPI on port 8799

Single doc — this file. `agents/agents.yaml` is the only other prose source (per-agent allowed/forbidden tools, memory scopes, termination contracts).

---

## Retrieval & Grounding

How we make a natural-language ticket land on the right code without fine-tuning the model. Two layers stacked, both KISS.

### Layer 1 — Retrieval (no training, already wired)

```
natural-language input
    │
    ▼
IntentLayer.classify()      Qwen 3.6 27B JSON-strict → action / entity /
                            reference_pattern / repo_hint / keywords
    │
    ▼
UnifiedContext.for_intent() fans out 8 sources, soft-fail per-source:

  ┌─ T1 :Episode      per-stage events            (write: api stage hooks)
  │                   "what did past tickets say"
  │
  ├─ T2 :Fact         canon facts, ground truth   (write: learner / chat retain)
  │                   "billing module = TransactionSyncRulesServiceImpl"
  │
  ├─ T3 :Pattern      learned recipes             (write: learner + pattern_miner)
  │                   "table-add tickets need 3 edits at consumer + rules + topic"
  │
  ├─ T4 :Chunk        code chunks + md ingests    (write: graphify; bge-m3 1024-d)
  │                   per-method bodies + per-section markdown
  │
  ├─ T5 :Symbol       tree-sitter + Graphify      (write: treesitter_ingest +
  │                   FQN, signatures, CALLS edges  graphify; ENDS WITH file_path)
  │
  ├─ :Repo standards  manifest                    (build/test/lint/conventions)
  │
  ├─ Aider RepoMap    PageRank tag digest         (process-local, mtime-cached)
  │
  └─ ripgrep -F + ctx exact lexical hits          (used by ref_pattern_split)

         │
         ▼
ContextBundle.render() → Markdown block injected into:
  - chat agent's first user message  (auto-prefetch)
  - planner.user_prompt              (## Edit targets, ## Reference snippets)
  - doer prompt                      (## Auto-context, ## Allowed files)
```

Input flows through all 8 sources, results are score-normalised, deduped on basename+parent, noise-filtered (target/, node_modules/, *.class, ...).

### Layer 2 — Hybrid lexical + semantic + graph

Three shapes of retrieval, each catches what the other misses.

| Shape | What it's good at | We have | Missing |
|---|---|---|---|
| **Lexical (BM25 / ripgrep)** | exact symbol names, literal strings (`businessProducts`), specific error messages | ripgrep + Postgres ILIKE | `pg_trgm` GIN index for fuzzy substring (e.g. typos) |
| **Dense (bge-m3 1024-d)** | natural-language → conceptually-related code; "how does login work" → `AuthFilter.doFilter` | T4 :Chunk + T5 :Symbol embeddings | cross-encoder rerank (bge-reranker-m3) on top-30 → top-5 |
| **Graph hop** | "what calls X" / "what reads collection Y" | 1-hop CALLS / IMPORTS / READS / WRITES | 2-hop expansion ("X→Y→Z") + Graphify INFERRED edges as a separate retrieval call |

The combination is what makes natural-language work without training. Lexical alone misses synonyms. Dense alone hallucinates near-misses. Graph alone has no entry point until you've found a symbol. Stacking all three covers the failure modes of each.

### Concrete gaps to close (cheap wins, no training)

| Gap | Fix | Effort | Status |
|---|---|---|---|
| No cross-encoder rerank | drop `bge-reranker-v2-m3` between unified_query top-30 → top-5; env: `AIFORGE_RERANK_URL` | ~30 min | ✅ shipped (a027ac7) |
| Synonym map per-repo | `<repo>/.aiforge/synonyms.yml`; IntentLayer expands query terms before vector search | ~30 min | ✅ shipped (e745fcc) |
| `.md / .adoc / runbook` not embedded | run `aiforge-maint docs ingest <repo> --library <repo>` (already plumbed) | one-shot | pending |
| Cross-repo CALLS edge | extend `treesitter_ingest` to walk all `WORKTREE_ROOT/*` repos; resolve external FQNs | ~2 h | pending |
| Symbol embed staleness | drop cron from 15min → 5min for active repos | env | pending |
| PR diff history not searchable | nightly `git log --patch` → `:Diff` nodes embedded | ~3 h | pending |

Each is a retrieval-side change. None needs a model change. None needs new infra beyond the rerank sidecar (Neo4j + Postgres + bge-m3 + bge-reranker now all present).

### Synonyms file format (`<repo>/.aiforge/synonyms.yml`)

```yaml
# Per-repo natural-language → code-identifier mapping. One pair per line.
# LHS = case-insensitive substring matched against the ticket body.
# RHS = space-separated tokens appended to intent.keywords.

sync rules:           TransactionSyncRulesServiceImpl
event listener:       DebeziumChangeEventConsumer
manual sync:          SyncDocumentController SyncOpsController
parent business:      fromBusinessId
propagate:            applyRulesForBusiness performFullGenericSync
```

Lookup order (first match wins, multiple files merge):
  1. `<repo>/.aiforge/synonyms.yml`
  2. `$AIFORGE_REPOS_BASE/.aiforge-global/synonyms.yml`
  3. `~/.aiforge/synonyms.yml`

Expansion is best-effort — missing files / parse errors are silently ignored.

### Rerank sidecar (services/rerank_sidecar)

Cross-encoder service backing `unified_query`'s rerank pass.

```
model:    BAAI/bge-reranker-v2-m3 (PyTorch, ~568 MB, FP16 by default)
runtime:  FlagEmbedding
endpoint: POST :8765/rerank   {query, texts:[…]} → {scores:[…]}
unit:     aiforge-rerank-sidecar.service (systemd --user)
install:  scripts/install-rerank-sidecar.sh   (idempotent venv + model download)
```

Score blending in `unified_query`:
```
final = 0.7 × rerank_score + 0.3 × source_weighted_original
```

The 0.3 source-weight retention preserves per-source priors (T2 fact > generic memory hit) so the rerank only reorders near-ties — it doesn't trump strong canonical hits. Sidecar absent / unreachable → no-op, original ranking stands.

### Acceptance-aware feedback gate (commit 37f6dad)

Three ticket shapes, three gates:

| Ticket shape | Detected by | Gate |
|---|---|---|
| Code edit (default) | none of the below | `edit_block_ok ≥ 1 AND compile_green ≥ 1` |
| Audit / report | body contains `audit-only`, `no production code changes`, `investigate only`, `report only`, `documentation only`, `do not change production code` | `compile_green ≥ 1` only — edit_block_ok skipped |
| Doc creation (NEW file) | title/body contains README/CHANGELOG/CONTRIBUTING/SECURITY/LICENSE/CODE_OF_CONDUCT AND intent.action ∈ add/edit/create/doc | `file_write OR diff has changes` (catches GA counter accounting bug) |

Doc-creation tickets also rewrite the planner's `## Files` block to a single canonical filename at repo root and inject a `## Doc-creation mode` section telling the doer to use `file_write` (not file_patch) and skip mvn compile.

These three rules unblock the audit + readme ticket classes that had been hitting `edit_block_ok=0` failures despite the doer doing the right thing.

### Why we don't fine-tune yet

Failure post-mortem on this session's 14 tickets:

| Bucket | Failure cause | Retrieval fix? |
|---|---|---|
| 5 done | n/a | n/a |
| 6 blocked | hardcoded gate (audit/doc) + path translation bug | code fix, not model |
| 6 cancelled | operator re-fire dups | UX, not model |
| 1 in-review | counter accounting bug | code fix |

None failed because the model didn't know the code. Every failure was a prompt-shape or gate bug. Retrieval has at least another 60% of the headroom before fine-tuning becomes the cheapest move.

### Order of operations if pushing further

```
1. ship reranker          (~30 min)  — biggest quality jump per hour
2. ship synonyms.yml      (~30 min)  — per-repo jargon mapping
3. ingest md docs         (1 cron)   — surfaces operator notes
4. cross-repo graph       (~2 h)     — multi-service ticket support
5. PR diff history        (~3 h)     — "how did we last add a collection"
6. (only after all of 1-5) consider LoRA on doer  — domain adaptation
```

---

## Ranking & Retrieval Pipeline (deep dive)

This is the most critical part of the system — what the model sees on every turn is decided here. Same algorithm runs for chat, planner, doer.

### End-to-end flow

```
natural-language input  ── IntentLayer.classify ──►  Intent
                                                       │
                                  synonyms.yml expand ─┤  (per-repo jargon → code FQNs)
                                                       │
                                                       ▼
                              ┌──────────────────────────────────────────┐
                              │  fan-out (parallel, soft-fail per src)   │
                              └──┬───┬───┬───┬───┬───┬───┬───┬──────────┘
                                 │   │   │   │   │   │   │   │
            unified_query 6 src  │   │   │   │   │   │   │   │
                ↓                │   │   │   │   │   │   │   │
   ┌──── memory T2 facts ────────┘   │   │   │   │   │   │   │
   │ ┌── ticket brief (T1)  ─────────┘   │   │   │   │   │   │
   │ │ ┌─ related sym (T5) ─────────────┘   │   │   │   │   │
   │ │ │ ┌ symbol vector (T5) ─────────────┘   │   │   │   │
   │ │ │ │  doc/find_doc (T4) ────────────────┘   │   │   │
   │ │ │ │  external lib docs ────────────────────┘   │   │
   │ │ │ │                                            │   │
   │ │ │ │  Aider RepoMap (T4/T5 PageRank) ───────────┘   │
   │ │ │ │  graph_neighbours (T5 edges) ──────────────────┘
   │ │ │ │  T3 :Pattern recipes (Memory.search tier='t3')
   │ │ │ │  similar_tickets (Postgres + bge-m3 cosine)
   │ │ │ │  repo_doc (CLAUDE.md / README.md tail)
   │ │ │ │  claude_memory (~/.claude/memory grep)
   ▼ ▼ ▼ ▼
 raw_hits                       (Each hit: {text, source, score, source_uri})
   │
   ▼
[A] Per-source score normalisation
       score' = (raw_score / max(raw_scores_in_source)) × source_weight
   │
   ▼
[B] Content dedup (240-char prefix fingerprint, lowercased)
       drops near-duplicate text from memory + related + symbol
   │
   ▼
[C] Path dedup (basename+parent identity)
       collapses /abs/.../X.java vs src/.../X.java
   │
   ▼
[D] Noise filter (target/, build/, *.class, *.pyc, .aiforge-worktrees/)
   │
   ▼
sort by score desc → take top 30
   │
   ▼
[E] Cross-encoder rerank (bge-reranker-v2-m3)
       POST :8765/rerank {query, texts:[…30]}  →  scores:[…30]
       blend: final = 0.7×rerank + 0.3×source_normalised
   │
   ▼
[F] take top-K (chat:5, planner:8, doer:12 by default)
   │
   ▼
ContextBundle.render() → prompt section
```

### Source catalogue (12 sources, with native score + weight)

| # | Source | Backend | Native score | Weight | Lookup mode |
|---|---|---|---|---|---|
| 1 | `memory` | Postgres `memories` table, hybrid (BM25 + bge-m3) | float [0,1] | 1.0 | `Memory().search(text, role, top_k)` |
| 2 | `ticket` | Postgres `tickets` (canonical row) + ticket_events tail | constant 1.0 | 1.2 | direct `id` lookup OR auto-detected `ONE-\d+` regex |
| 3 | `related` | graph_rag MCP `related_memories` | float | 0.8 | Cypher hop on `:Memory` graph |
| 4 | `symbol` | graph_rag MCP `sym_lookup` | float | 0.9 | Neo4j `:Symbol` vector index (bge-m3 1024-d cosine) |
| 5 | `doc` | graph_rag MCP `find_doc` | float | 0.6 | Neo4j fulltext on `:Memory.text` markdown wing |
| 6 | `external` | sqlite `docs_index` | float | 0.4 | bge-m3 vector over external lib docs (spring/react/mongo/...) |
| 7 | `aider_repomap` | Aider tree-sitter PageRank | rank position | 1.0 | local in-process call, mtime-cached, focal_files seeded |
| 8 | `graph_neighbours` | Neo4j `:Symbol` CALLS/IMPORTS/EXTENDS | edge count | 0.9 | Cypher 1-hop expansion from focal_files |
| 9 | `t3_patterns` | `Memory.search` filtered to `tier='t3'` | float [0,1] | 0.85 | learner-written + auto-promoted recipes |
| 10 | `similar_tickets` | Postgres tickets + bge-m3 cosine | cosine [0,1] | 0.7 | ILIKE prefilter (60 cands) → embed_batch → cosine |
| 11 | `repo_doc` | filesystem read of `CLAUDE.md` / `README.md` | constant 1.0 | 0.5 | first 1500 chars from worktree root |
| 12 | `claude_memory` | grep `~/.claude/memory/*.md` | line count | 0.4 | regex over operator memory |

### Step-by-step ranking math

#### [A] Per-source normalisation

```python
# aiforge_core/context/unified.py:_normalise_hits
for src, hits_in_src in groupby(raw_hits, key="source"):
    max_s = max(h["score"] for h in hits_in_src) or 1.0
    weight = _DEFAULT_SOURCE_WEIGHTS[src]
    for h in hits_in_src:
        h["score"] = (h["score"] / max_s) × weight
```

Effect: every source's hits get rescaled to `[0, weight]`. The weight is the prior — ticket_brief (1.2) outranks external lib docs (0.4) even when the external doc has a "better" raw cosine, because the canonical ticket is more authoritative for a ticket-shaped question.

Worked example — query *"add storeRegions sync"*:

```
raw_hits before normalise:
  symbol      DebeziumChangeEventConsumer       0.91
  symbol      TransactionSyncRulesServiceImpl   0.88
  symbol      Random.nextInt                    0.42
  memory      "add Parties in line 86 of ..."   0.74
  doc         spring docs intro                 0.65
  external    mongodb-docs aggregation          0.81

after normalise (max in source × source_weight):
  symbol/0.91 → 1.00 × 0.9 = 0.900    DebeziumChangeEventConsumer
  symbol/0.88 → 0.97 × 0.9 = 0.870    TransactionSyncRulesServiceImpl
  symbol/0.42 → 0.46 × 0.9 = 0.415    Random.nextInt           ← still ranked low
  memory/0.74 → 1.00 × 1.0 = 1.000    "add Parties..."
  doc/0.65   → 1.00 × 0.6 = 0.600    spring docs
  external/0.81→ 1.00 × 0.4 = 0.400  mongodb-docs aggregation  ← weight pushes down
```

#### [B] Content dedup

```python
fingerprint = text[:240].lower().strip()
if fingerprint in seen: drop
```

Catches near-duplicates: same fact from `memory` + `related` + `symbol` collapsing to one hit. Avoids the LLM seeing the same paragraph 3× and wasting context.

#### [C] Path dedup

```python
key = "/".join(parts[-2:])  # parent_dir/basename
```

`/home/mani/codeRepo/X/src/.../Foo.java` and `src/.../Foo.java` both produce `feature/Foo.java` and dedupe correctly.

#### [D] Noise filter

`aiforge_core/indexing/noise.py` — single source of truth used by indexers AND retrievers. Drops:

- dirs: `target`, `build`, `out`, `dist`, `node_modules`, `vendor`, `.gradle`, `.mvn`, `.aiforge-worktrees`, `__pycache__`, ...
- extensions: `.pyc`, `.class`, `.jar`, `.so`, `.lock`, `.min.js`, `.png`, ...

Defense in depth — what's invisible to ingest is invisible to retrieval, and vice versa.

#### [E] Cross-encoder rerank

```python
# aiforge_core/memory/unified_query.py:_rerank_top
top_30 = sorted_hits[:30]
scores = POST :8765/rerank {query: full_text, texts: [h.text[:1500] for h in top_30]}
for h, s in zip(top_30, scores):
    h["score"] = 0.7 × s + 0.3 × h["score"]      # blend
sorted_hits = sorted(top_30, by score desc) + sorted_hits[30:]
```

The blend is the critical design choice:

- **0.7×rerank** — cross-encoder sees query+text together, captures full semantic match. Dominates the score.
- **0.3×source_weighted** — preserves the prior (T2 fact > generic memory > external doc). Stops a high-scoring but low-trust hit from beating a canonical fact at near-tie.

Without the blend (pure rerank): a noisy generic hit scoring 0.95 would beat a T2 canonical fact scoring 0.90. With the blend: `0.7×0.95 + 0.3×0.5 = 0.815` vs `0.7×0.90 + 0.3×1.2 = 0.99` — fact wins.

Sidecar absent / unreachable → step E is a no-op, ranking falls back to A-D order.

#### [F] Top-K take

| Caller | K | Why |
|---|---|---|
| chat | 5 | render budget ~3K tokens; chat answers tend short |
| planner | 8 | needs more breadth to write the right plan |
| doer | 12 | edit_targets + reference_files + memory hits + commands all displayed |

### Synonyms expansion (how it composes with ranking)

```
ticket body: "change the sync rules to propagate parent business changes"
                                ↓
             classify() → entity='propagate', keywords=['sync','rules','parent']
                                ↓
             expand from <repo>/.aiforge/synonyms.yml:
               'sync rules' → TransactionSyncRulesServiceImpl
               'propagate'  → applyRulesForBusiness performFullGenericSync
               'parent business' → fromBusinessId
                                ↓
             intent.keywords now includes the 5 added FQNs
                                ↓
             fan-out queries every source with the expanded query
                                ↓
             symbol vector search hits the actual code (not just the user's phrase)
                                ↓
             reranker re-orders by full natural-language match
```

Synonyms multiply the recall (more queries → more candidates) without harming precision (rerank filters noise). A miss in synonyms.yml = unchanged from baseline; a hit = recall lift on jargon-heavy bodies.

### Failure modes and what happens

| Failure | Effect on ranking |
|---|---|
| bge-m3 sidecar down | sources 1, 4, 6, 10 silently no-op; lexical (memory ILIKE, ripgrep) still works |
| bge-reranker sidecar down | step E skipped, ranking falls back to source-weighted normalised order |
| Neo4j down | sources 3, 4, 5, 8, 9 no-op; sources 1, 6, 11, 12 still work |
| Postgres down | sources 1, 2, 10 no-op; sources 6, 7, 8, 11, 12 still work |
| synonyms.yml missing | classifier output unchanged, no expansion |
| empty query | early return `{hits: [], used_sources: [], errors: []}` — never crashes |

Soft-fail is mandatory at every layer. The pipeline reports what it used (`bundle.sources_used`) so the prompt downstream knows whether retrieval was full-stack or degraded.

### A/B verdict: Cursor vs Aider retrieval

Tested both approaches on real PosClientBackend and mongoEventListner
queries (commit ce31f73). Tool: `aiforge ticket retrieval-eval`.

| Test | Cursor (vector + reranker) | Aider (PageRank + mentions) |
|---|---|---|
| "Add 3 REST APIs to PosClientBackend mirroring product feature" | **0 hits** | 8 hits — BusinessProductsController, TransactionSyncRule(Service+Impl), WarehouseController |
| "Add storeRegions collection event listening to mongoEventListner" | **0 hits** | 8 hits — TransactionSyncRulesService, DebeziumEventParser |
| Latency | ~400ms | ~1000ms |

**Aider wins today** because:
- Operates on tree-sitter tags computed at query time over the
  worktree — no backfill cron required.
- aider's `mentioned_idents` (every word of user text via
  `re.split(r"\W+", text)`) + `mentioned_fnames` (basename match
  against repo) feed PageRank personalisation. The graph centres
  on what the user said, ranks neighbours by edge structure.

**Cursor approach blocked on**: per-repo method/class embedding backfill.
Today only `oneshell-business` has 3088 method embeddings; PosClientBackend
and mongoEventListner have 0. The 15-min `aiforge-symbol-embed.timer`
will populate the rest over ~8 hours.

**Decision**: aider is now the primary retriever (UnifiedContext source
#7 with `user_text` plumbed end-to-end). Cursor-style stays as an
opportunistic fallback — `_semantic_focal_files` runs only when the
focal_files extraction returns empty AND a vector index has entries
for the target repo. When backfill catches up across repos, cursor
becomes a true second-stage retriever; until then it's a no-op for
most repos.

### Tuning knobs

| Env | Default | Purpose |
|---|---|---|
| `AIFORGE_UMEM_WEIGHT_<SOURCE>` | per-source default | override any `_DEFAULT_WEIGHTS` entry at runtime |
| `AIFORGE_RERANK_URL` | `http://127.0.0.1:8765` | cross-encoder endpoint |
| `AIFORGE_RERANK_DISABLE` | `0` | force step E off (debugging) |
| `AIFORGE_AIDER_REPOMAP_TOKENS` | `1024` | budget for source #7 |
| `AIFORGE_DOER_NEIGHBOURS_LIMIT` | `30` | max edges from source #8 |

Source weights are deliberately not in a yaml — they're config-as-code so a misnamed key fails fast at import. Override per-source via env when A/B testing the prior.
