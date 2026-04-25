# Architecture (2 machines, v5 — 2026-04-25)

Two hosts, one laptop for dev. Same physical layout as v4; the runtime stack underneath is rebuilt.

## What changed v4 → v5

| Layer | v4 (2026-04-24) | v5 (this doc) |
|---|---|---|
| Orchestrator | LangGraph state machine, custom checkpointer | Google ADK (`adk-python`) — `BaseAgent` subclasses, `SequentialAgent` workflow, `BasePlugin.on_event_callback` for auto-remember, `DatabaseSessionService` Postgres-backed sessions |
| Inner agent loop | smolagents `CodeAgent` / `ToolCallingAgent` | GenericAgent (`lsdefine/GenericAgent`) text-protocol session, embedded via custom `BaseAgent` calling `agent_runner_loop` |
| LLM serving | LM Studio app (single instance, multi-JIT) | `mlx_lm.server` direct, two pinned instances (doer :1234, planner :1235), launchd-managed |
| Code map | `~/.claude/memory` markdown + ad-hoc grep | Aider RepoMap (PageRank tree-sitter digest, hot path) + Graphify nightly graph.json (cold/cross-repo) |
| Graph ingest | manual `file-indexer` python | Tree-sitter direct AST → `:File`/`:Symbol`/`:CALLS`/`:IMPORTS` (25 langs) |
| Memory injection | one flat `:Fact` recall step | 5 GA-style layers (L0…L5), each with a defined storage + injection rule |
| Ranking | external Python reranker over pgvector | Native Neo4j Cypher fusing vector index + Lucene fulltext + 2-hop graph boost — single query, no GDS |
| Roles | planner / doer / feedback / learner (smolagents-flavoured) | Architect (Claude Code, ext) → Planner → Doer → Feedback → Learner, each with explicit tool allowlist + max-turn cap |

Smolagents tool_calls JSON parsing is dead — `mlx_lm` 0.31 emits malformed `tool_calls` in 8-bit mode. GA's text-protocol session ("Action: …\nObservation: …") sidesteps it entirely.

## Topology

```
Mac Studio  192.168.70.185                 NUC  192.168.70.191 (static)
(LLM + agents, 96 GB unified)              (storage + services, 30 GB RAM)
─────────────────────────────              ────────────────────────────────
mlx_lm.server  :1234  doer                 Postgres 16            :5432
  qwen-coder-next-mlx-4bit          22 GB    aiforge db (tickets, memories,
mlx_lm.server  :1235  planner                ADK sessions, checkpoints)
  Qwen3.6-27B-UD-MLX-4bit           18 GB
nomic-embed-text-v1.5  (768d)               Neo4j 5.26 Community  :7474/:7687
  served via mlx_lm /v1/embeddings           - vector idx :Fact.embedding (768, cos)
                                             - fulltext idx :Fact.text (Lucene)
ADK runtime (Python 3.12)                    - :File / :Symbol / :CALLS / :IMPORTS
  SequentialAgent
    Architect (ext)                         oneshell-mcp servers
    Planner   (BaseAgent → GA loop)           QA   :8810-8813
    Doer      (BaseAgent → GA loop)           prod :8820-8823
    Feedback  (LiteLLM, 1 turn)
    Learner   (LiteLLM, 1 turn)              systemd --user timers
  AiForgeHandler(GenericAgentHandler)         ├─ repo-pull         5 min
    _get_abs_path  → ScopeGuard               ├─ memory-pull       5 min
    tool_before    → forbidden tools          ├─ git-pull         10 min
    turn_end       → Neo4j L4 mirror          ├─ markdown-ingest  30 min
    do_ask_explorer (RAG fallback)            └─ reindex-daily    02:00
                                               (graphify clone + load)
Aider RepoMap (lib, in-process)
  SQLite cache  ~/.aider/cache.db            lm-tunnel.service
  injects ~1.5K tok digest into Doer           ssh -L 1235→MS:1234
                                               ssh -L 1236→MS:1235
Graphify CLI  (graphifyy from PyPI)
  nightly:  graphify scan → graph.json       Direct LAN 10.10.10.1 ↔ .2
            loader → NUC Neo4j               ~0.6 ms RTT, 1 GbE

launchd                                      Postgres tunnel back-channel:
  com.aiforge.mlx-doer.plist                   MS opens ssh -L 5433→NUC:5432
  com.aiforge.mlx-planner.plist                MS opens ssh -L 7688→NUC:7687
  com.aiforge.adk-runner.plist                 (macOS Sequoia sandboxes raw LAN
  caffeinate (wake lock)                        connect(); loopback works)
  pg-tunnel (5433→NUC:5432)
  neo4j-tunnel (7688→NUC:7687)
```

## Who owns what

| Concern | Host | Component |
|---|---|---|
| Doer LLM inference | Mac Studio | `mlx_lm.server :1234` qwen-coder-next-mlx-4bit |
| Planner LLM inference | Mac Studio | `mlx_lm.server :1235` Qwen3.6-27B-UD-MLX-4bit |
| Embeddings (768d, nomic) | Mac Studio | `mlx_lm.server` `/v1/embeddings` |
| ADK orchestrator (SequentialAgent) | Mac Studio | `aiforge_core/runtime/adk_runner.py` |
| Inner agent loop (text-protocol) | Mac Studio | GenericAgent `agent_runner_loop` via `aiforge_core/doer/agent.py`, `aiforge_core/planner/agent.py` |
| ScopeGuard / forbidden tools | Mac Studio | `AiForgeHandler` overrides in `aiforge_core/doer/handler.py` |
| Aider RepoMap (hot code digest) | Mac Studio | `aiforge_core/index/aider_map.py` (lib in-process), SQLite at `~/.aider/cache.db` |
| Graphify CLI (cold graph build) | Mac Studio | `graphify scan` → `graph.json`; loader at `aiforge_core/index/graphify_loader.py` writes to NUC Neo4j |
| Tree-sitter ingest (incremental) | Mac Studio | `aiforge_core/index/treesitter_ingest.py` (post-merge hook) |
| Java repo worktrees + `mvn compile` | Mac Studio | per-ticket worktree under `~/codeRepo-worktrees/` |
| Acceptance gate | Mac Studio | `aiforge_core/doer/acceptance_gate.py` |
| Postgres (tickets, memories, ADK sessions, checkpoints) | NUC | `aiforge` db, `DatabaseSessionService("postgresql+asyncpg://…")` |
| Neo4j (graph + vector + fulltext) | NUC | single DB, all 5 memory layers' Neo4j-resident parts |
| oneshell-mcp servers (8 instances, QA + prod) | NUC | `:8810-8813` QA, `:8820-8823` prod |
| Markdown ingest crons | NUC | systemd `--user` timers (migrated from MS launchd) |
| Git pulls (`~/codeRepo/*`, `~/.claude/memory`) | NUC | `aiforge-repo-pull.timer` |

## Roles & per-agent rules

Each role is a separate ADK `BaseAgent` subclass. The pipeline is one `SequentialAgent` per ticket; sub-tickets fan out to a parallel Doer wave when independent.

| Role | Model | Backend | Max turns | Max wall | Tool allowlist | Memory scope |
|---|---|---|---|---|---|---|
| Architect | Claude Code (laptop, external) | n/a | n/a | n/a | writes tickets only, never edits code | reads L2/L3, writes tickets to Postgres |
| Planner | Qwen3.6-27B :1235 | GA text-protocol (CodeAgent-style until EVAL-1b) | 12 | 8 min | `read_file`, `ask_explorer`, `cypher_query`, `recall_similar_flows`; emits sub-tickets ≤ 3 files; **no code edit, no ask_user** | L2 facts + L3 SOPs auto-injected into system prompt |
| Doer | qwen-coder-next :1234 | GA text-protocol | 40 | 25 min | `read_file`, `edit_block`, `apply_patch`, `run_tests`, `ask_explorer`; **scope-guarded ≤ 3 files per sub-ticket; no ask_user, no start_long_term_update** | L2 + L5 (Aider digest) auto-injected; L4 recall via tool |
| Feedback | qwen-coder-next | direct LiteLLM | 1 | 60 s | none — verdict only | reads doer turn output + acceptance-gate result |
| Learner | qwen-coder-next | direct LiteLLM | 1 | 60 s | writes `:Fact` only via ADK `on_event_callback` hook; **no free-form output** | reads full session, writes L2 |

Forbidden-tool enforcement is in `AiForgeHandler.tool_before_callback`. ScopeGuard rejects writes outside the sub-ticket's declared file list inside `_get_abs_path`. Both raise back into the GA loop as observations, so the model self-corrects rather than crashing.

## Memory layers (5, GA convention)

| GA layer | Storage | Neo4j label | Auto-injected? | Tool to read |
|---|---|---|---|---|
| L0 META-SOP | NUC Neo4j | `:MetaSop` | Learner only (when proposing new SOP) | `recall_meta_sop` |
| L1 working memory | ADK Session, in-mem on MS, mirrored to L4 on turn-end | n/a | current agent only | implicit (session state) |
| L2 global facts | NUC Neo4j | `:Fact` (768d vector + fulltext) | Doer + Planner system prompts (top-K hybrid) | `recall_facts(query)` |
| L3 task SOPs | NUC Neo4j | `:Sop` | Planner only, conditional on ticket type tag | `recall_sop(tag)` |
| L4 raw sessions (auto-remember) | NUC Neo4j | `:Session` ←[:HAS_TURN]→ `:Turn` | not injected — recall on demand | `recall_similar_flows(query)` |
| L5 code structure | NUC Neo4j (`:File`/`:Symbol`/`:CALLS`/`:IMPORTS`) + Aider SQLite hot cache + Graphify `graph.json` mirror | various | Doer **always** (Aider 1.5K-tok digest) | `cypher_query(…)` for cross-file traversal |

Auto-remember is the `BasePlugin.on_event_callback` on the runner: every `Event` (turn end, tool call, tool result) gets persisted as a `:Turn` under the current `:Session`. Cost is one Cypher MERGE per event, batched. `aiforge_core/runtime/auto_remember_plugin.py`.

## Ranking (one Cypher query, no separate ranker)

The `recall_facts(query)` tool runs:

```cypher
CALL db.index.vector.queryNodes('fact_embedding', $k, $qVec)  YIELD node AS f, score AS vScore
WITH collect({f:f, v:vScore}) AS vHits
CALL db.index.fulltext.queryNodes('fact_text',     $qText)    YIELD node AS f, score AS tScore
WITH vHits, collect({f:f, t:tScore}) AS tHits
// fuse
UNWIND vHits + tHits AS h
WITH h.f AS f,
     coalesce(max(h.v),0) * 0.7 AS vs,
     coalesce(max(h.t),0) * 0.3 AS ts
// 2-hop boost if fact ABOUT current ticket
OPTIONAL MATCH (f)-[:ABOUT*1..2]-(t:Ticket {id:$ticketId})
WITH f, vs + ts + (CASE WHEN t IS NULL THEN 0 ELSE 0.2 END) AS score
RETURN f.text AS text, score
ORDER BY score DESC LIMIT $k
```

- Vector: native Neo4j 5 vector index, cosine, dim 768 — weight **0.7**
- Keyword: Lucene fulltext index on `:Fact.text` — weight **0.3**
- Graph hop: +**0.2** if `:Fact -[:ABOUT*1..2]- :Ticket{id=current}`

No GDS, no PageRank step, no external reranker. Escalation rule: if eval precision drops below 0.7 on the F-suite, add a cross-encoder rerank stage; not before. Lives in `aiforge_core/recall/hybrid.py`.

## Cross-host bridges

Same as v4: only ssh tunnels.

- `com.aiforge.pg-tunnel` (MS): `ssh -L 127.0.0.1:5433:127.0.0.1:5432 mani@10.10.10.2` — ADK `DatabaseSessionService` and the API hit Postgres via MS-loopback.
- `com.aiforge.neo4j-tunnel` (MS): `ssh -L 127.0.0.1:7688:127.0.0.1:7687 mani@10.10.10.2` — `recall_facts` and `cypher_query` hit Neo4j via MS-loopback (Sequoia LAN sandbox again).
- `lm-tunnel.service` (NUC): `ssh -L 127.0.0.1:1235:127.0.0.1:1234 manikanta@10.10.10.1` plus `:1236→:1235` — NUC-side scripts (markdown ingest, eval harness) reach MS mlx_lm via NUC-loopback.

## Data flow

GitHub is source of truth. Both hosts `git pull` directly. Zero rsync between hosts.

```
GitHub ──pull──> NUC:~/codeRepo/*           every 5 min (systemd timer)
GitHub ──pull──> MS:~/codeRepo-worktrees/*  per-ticket, on demand
laptop ──push──> GitHub (AIForgeCrew)       both hosts pull every 10 min

NUC nightly 02:00:
  graphify clone <each repo>
  graphify scan → graph.json
  graphify_loader.py → Neo4j (:File / :Symbol / :CALLS / :IMPORTS, MERGE-by-id)
```

Aider RepoMap is built **on the MS, in the Doer process**, on first call per worktree, and cached in `~/.aider/cache.db` keyed by file mtime. The 1.5K-token digest is recomputed only when files in the sub-ticket scope change. It is **not** mirrored to Neo4j — Neo4j gets the full Graphify dump nightly; Aider is the hot path for the current sub-ticket.

## File map (where v5 components live)

| Component | Path |
|---|---|
| ADK runner / SequentialAgent wiring | `aiforge_core/runtime/adk_runner.py` |
| Auto-remember plugin (L4) | `aiforge_core/runtime/auto_remember_plugin.py` |
| Planner BaseAgent + GA bridge | `aiforge_core/planner/agent.py` |
| Planner tools (recall_sop, etc.) | `aiforge_core/planner/tools.py` |
| Doer BaseAgent + GA bridge | `aiforge_core/doer/agent.py` |
| Doer GA handler (ScopeGuard, Neo4j mirror, ask_explorer) | `aiforge_core/doer/handler.py` |
| Acceptance gate | `aiforge_core/doer/acceptance_gate.py` |
| Orchestrator bridge (sub-ticket fan-out) | `aiforge_core/doer/orchestrator_bridge.py` |
| Hybrid recall (vector + fulltext + graph) | `aiforge_core/recall/hybrid.py` |
| Aider RepoMap wrapper | `aiforge_core/index/aider_map.py` |
| Graphify loader (graph.json → Neo4j) | `aiforge_core/index/graphify_loader.py` |
| Tree-sitter incremental ingest | `aiforge_core/index/treesitter_ingest.py` |
| FastAPI surface | `aiforge_core/runtime/api.py` |
| Per-agent config (allowlists, caps) | `aiforge_core/agents.yaml` |
| launchd plists (mlx_lm + ADK + tunnels) | `scripts/runtime/com.aiforge.*.plist` |
| Eval fixtures (F1, F3, …) | `evals/fixtures/` |

## Boundary rules (still enforced)

1. **Architect never edits code.** Tickets in Postgres only. Enforced socially + by the agent allowlist (no edit tools).
2. **Doer scope ≤ 3 files**, declared in the sub-ticket. ScopeGuard in `_get_abs_path` is the chokepoint. Glob entries (`foo/bar/**`, `foo/*.ext`) supported per commit `57e1e7c`.
3. **No ask_user from Planner or Doer.** Both have `ask_explorer` (RAG over Neo4j) instead. Forbidden in `tool_before_callback`.
4. **No long-term memory writes from Doer.** Only Learner writes `:Fact`, and only via the ADK on-event hook — never as a free-form tool call.
5. **Single MLX model per port.** Don't multiplex; the launchd plist pins exactly one model per `mlx_lm.server` instance. Idle-unload disabled (`--ttl 43200` lesson from EVAL-3).
6. **All inter-host I/O is ssh-tunneled loopback.** No raw LAN connect from macOS apps.
