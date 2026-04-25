# Stack (what + why) — v5

## v4 → v5 changes

- **LM Studio → `mlx_lm.server`** (direct). Two instances managed by `launchd`:
  `com.aiforge.mlx-doer` on `:1234` and `com.aiforge.mlx-planner` on `:1235`.
  No GUI, no electron, no model-juggling daemon — one MLX process per role,
  pinned to a single weight, restart-on-crash via launchd.
- **smolagents → GenericAgent (Doer only).** Doer now runs
  [lsdefine/GenericAgent](https://github.com/lsdefine/GenericAgent)'s
  `agent_runner_loop` (text-protocol, not OpenAI native tool_calls) wrapped
  inside a custom `BaseAgent` subclass. Planner stays on smolagents
  `CodeAgent` until EVAL-1b says otherwise.
- **LangGraph → Google ADK** (`google-adk` PyPI). `BaseAgent` subclasses +
  `SequentialAgent` orchestrator. `BasePlugin` for cross-cutting hooks
  (Neo4j fact mirror, OpenTelemetry spans, telemetry counters).
- **LangGraph PostgresSaver → ADK `DatabaseSessionService`**
  (`postgresql+asyncpg://…`). Same Postgres, different writer; sessions live
  in ADK-native tables.
- **LlamaIndex pgvector → dropped.** Vectors moved to Neo4j native vector
  index on `:Fact.embedding`. Single Cypher does hybrid retrieval.
- **+aider-chat** PyPI — `RepoMap` for the per-call code-context digest in
  the Doer system prompt.
- **+graphifyy** PyPI — nightly three-pass codebase graph builder
  (AST + Whisper + Claude subagents) → `graph.json` → Neo4j mirror.
- **+neo4j** Python driver — direct Cypher client used by ADK plugins,
  ingest scripts, and query tools.
- **+OpenTelemetry** — built into ADK (`telemetry/tracing.py`); spans for
  agent invocation, model calls, tool calls. Drop-in for any OTel collector.
- **LiteLLM** — kept, but scope narrowed: now used **only** by Planner,
  Feedback, and Learner. Doer talks to MLX directly through GenericAgent's
  `LLMSession` / `NativeToolClient` (text-protocol path), to dodge the
  `mlx_lm` 0.31 `tool_calls` serialization bug.

---

## Major tools / libraries

### LLM serving + tool loops

| Tool | PyPI / source | What for |
|---|---|---|
| **`mlx_lm.server`** | `mlx-lm>=0.31` | Two instances on Mac Studio: `:1234` (Doer / `qwen3-coder-next-mlx-4bit`), `:1235` (Planner / `qwen3.6-27b-mlx-4bit`). OpenAI-compat. Managed by launchd plists `com.aiforge.mlx-doer.plist` + `com.aiforge.mlx-planner.plist`. |
| **GenericAgent** | github.com/lsdefine/GenericAgent (vendored at `~/genericagent/`) | Doer tool loop. We embed `agent_runner_loop` inside our `BaseAgent` subclass and use the text-protocol path (`<tool>…</tool>` blocks) via `LLMSession` / `NativeToolClient` — avoids the mlx_lm 0.31 native-tool serialization bug. |
| **smolagents** | `smolagents>=1.14` | Planner runs `CodeAgent` (kept until EVAL-1b). |
| **LiteLLM** | `litellm>=1.55` | Uniform OpenAI-shaped client to MLX servers. Used by Planner (via smolagents), Feedback, and Learner. **Not** used by Doer. |
| **nomic-embed-text-v1.5** | served by an MLX-embedding sidecar | 768-d embeddings. Same model as v4; only the host changed. |

### Agent orchestration + plugins

| Tool | PyPI | What for |
|---|---|---|
| **Google ADK** | `google-adk>=0.4` | `BaseAgent` subclasses for Architect / Planner / Doer / Feedback / Learner. `SequentialAgent` is the top-level orchestrator that replaces our LangGraph state machine. |
| **ADK `BasePlugin`** | (in `google-adk`) | Cross-cutting hooks: Neo4j fact mirror on session-end, OpenTelemetry span emission, ticket-event counters. |
| **ADK `DatabaseSessionService`** | (in `google-adk`) | Replaces LangGraph `PostgresSaver`. Connects via `postgresql+asyncpg://aiforge@nuc:5432/aiforge`. Session resume, partial-tick replay. |
| **OpenTelemetry** | bundled in `google-adk` (`telemetry/tracing.py`) | Spans: `agent.invoke`, `model.call`, `tool.call`. OTLP-compatible — point at any collector. |

### Code-context + graph

| Tool | PyPI | What for |
|---|---|---|
| **aider-chat** | `aider-chat>=0.70` | We import `aider.repomap.RepoMap` directly. Hot-path code-context digest for the Doer system prompt. Tree-sitter via `grep-ast`, SQLite cache at `~/.aider.tags.cache.v3`, token-budgeted PageRank-ranked output. Recomputed on every Doer call. |
| **grep-ast** | `grep-ast>=0.3` | Aider's tree-sitter wrapper. Pulled in transitively but pinned explicitly so we control parser versions across Java/TS/Python. |
| **graphifyy** | `graphifyy>=0.2` | Three-pass codebase graph builder (AST + Whisper transcript + Claude subagents). Nightly cron on NUC produces `graph.json` (NetworkX). A loader script imports it into Neo4j as `:File` / `:Symbol` / `:CALLS` mirror. Supports `graphify clone <repo>` and `graphify merge-graphs` for cross-repo overlays. |
| **neo4j (driver)** | `neo4j>=5.26` | Direct Cypher client. Used by ADK plugins (fact mirror), ingest scripts (graphify loader, JavaParser/ts-morph/libcst pipelines), and the query tools exposed to Doer. |

### Code parsers (→ graph, unchanged from v4)

| Tool | What for |
|---|---|
| **JavaParser** (shaded jar, built via Maven) | Java AST: classes, methods, calls, Spring annotations |
| **ts-morph** | TypeScript/React AST: components, hooks, fetch URLs |
| **libcst + ast** | Python: FastAPI/Flask routes, pymongo, env reads |
| **SCIP indexers** | Cross-language symbol + ref index (used by graphify pass 1) |
| **tree-sitter-java** | Older fast-path parser; kept for incremental updates |

> v5 note: parsers still feed Neo4j directly for the canonical graph.
> Aider RepoMap and Graphify are *additive* — they produce overlays
> (per-call digest, nightly mirror) that ride alongside the canonical
> ingest, not replace it.

### Databases

| Tool | What for |
|---|---|
| **Neo4j 5.26 Community + APOC + genai** | One DB does graph, vector (native `:Fact.embedding` index), and BM25 fulltext. **No GDS.** Already running on NUC; unchanged from v4. |
| **PostgreSQL 16 + pgvector + pg_trgm + pgcrypto** | Tickets, events, ADK sessions, audit log. `pgvector` retained because some legacy event payloads still index by embedding; the live retrieval path moved to Neo4j. |

### Runtime + plumbing

| Tool | What for |
|---|---|
| **FastAPI / uvicorn** | `http://NUC:8799` REST: tickets, events, health |
| **psycopg v3** | Postgres client (sync paths) |
| **asyncpg** | ADK `DatabaseSessionService` async driver |
| **pymongo** | Mongo client (for the `mongo_agent` tool exposed to Doer) |
| **httpx** | HTTP client (embed + LLM calls to MLX) |
| **MCP stdio** | Protocol for exposing graph-RAG tools to the LLM |
| **Docker / OrbStack** | Container for Neo4j on NUC |
| **systemd --user** | NUC services + timers (`aiforge-*`, `graphify-nightly.timer`) |
| **launchd** | Mac Studio services: `com.aiforge.mlx-doer`, `com.aiforge.mlx-planner` |
| **ssh -L tunnels** | pg-tunnel (MS ↔ NUC pg), neo4j-tunnel, embed-tunnel |
| **ufw / nmcli** | NUC firewall + static IP |
| **networksetup** | Mac Studio static IP on direct-LAN NIC |
| **gh CLI** | PRs + GitHub API |

### Dev

| Tool | What for |
|---|---|
| **uv** | Python venv + deps |
| **pytest + ruff** | Tests + lint |
| **Maven + OpenJDK 21** | Build the JavaParser jar on NUC |
| **mvn** (on MS) | Run by Doer inside the ticket worktree for `run_compile` |

---

## Per-agent backend mapping

| Agent | Backend | Model | Library |
|---|---|---|---|
| **Architect** | external | Claude Code | n/a |
| **Planner** | smolagents `CodeAgent` (until EVAL-1b) | Qwen3.6-27B-MLX-4bit @ `:1235` | `smolagents` + `litellm` |
| **Doer** | GenericAgent text-protocol | Qwen3-Coder-Next-MLX-4bit @ `:1234` | `agent_runner_loop` wrapped in ADK `BaseAgent`; direct `LLMSession` / `NativeToolClient` |
| **Feedback** | direct LiteLLM single-shot | qwen-coder-next @ `:1234` | `litellm` |
| **Learner** | direct LiteLLM single-shot | qwen-coder-next @ `:1234` | `litellm`, writes via ADK plugin → Neo4j |

Why GenericAgent for Doer specifically: `mlx_lm` 0.31 has a known
`tool_calls` field-serialization bug under load (drops or duplicates
`id` fields). GenericAgent's text-protocol path keeps tool calls inside
the assistant message body (`<tool>…</tool>`), so the bug is bypassed
without giving up structured tool use. EVAL-2 confirmed parity vs. native
on green-path Java fixtures.

---

## Ranking strategy

One Cypher query, no rerankers, no GDS, no application-side fusion gymnastics.

```text
score = 0.7 * vector_cosine(query_emb, fact.embedding)
      + 0.3 * fulltext_bm25(fact.text, query)
      + 0.2 * graph_hop_boost     # +0.2 if fact is within 2 hops of current ticket
```

- **No external reranker.** Profiled cross-encoders cost more latency
  than they recover NDCG on our corpus.
- **No GDS.** Plain Cypher + the native vector + fulltext indexes are
  enough at our scale (single-tenant, low-six-figures `:Fact` nodes).
- **No PageRank in the retrieval path.** Aider does its own PageRank
  internally for repo-map; we don't double-rank.

---

## Why two machines (unchanged)

One load profile per host.

| Host | Load | Why own machine |
|---|---|---|
| **Mac Studio** (96 GB unified, M3 Ultra) | Two `mlx_lm.server` instances + KV cache + mvn compile | LLM inference pins huge RAM; Metal GPU is Apple-silicon only; Java builds need throughput |
| **NUC 11** (30 GB, i7) | Postgres + Neo4j + FastAPI + indexers + graphify nightly + git pulls | Always-on 24/7; cheap RAM for DBs; restarts don't interrupt LLM serving |
| Laptop | Dev shell, queries | Not part of runtime |

Splitting them means:

- Bouncing the API doesn't unload a 38 GB model.
- Doer's MLX (`:1234`) and Planner's MLX (`:1235`) live in separate
  processes, so a Doer crash never blows away the Planner's KV cache —
  launchd restarts only the affected role.
- NUC can reindex code overnight (graphify + JavaParser + ts-morph +
  libcst) without touching LLM latency.
- Either host can reboot alone; only cross-host glue is the ssh
  tunnels (pg, neo4j, embed).
