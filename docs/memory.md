# Memory

How AIForgeCrew remembers things and how each agent uses memory.

## TL;DR

Six memory sources fused into the Doer prompt. Five canonical layers
all but L1 stored in **NUC Neo4j**, plus two code-index helpers that
sit in front of L5 (Aider RepoMap and Graphify graph neighbours). Each
source has a single purpose and an explicit injection rule.

```
┌─────────────────────────────────────────────────────────────────┐
│                          NUC Neo4j                               │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌────────┐ │
│  │  L0     │  │   L2    │  │   L3    │  │   L4    │  │   L5   │ │
│  │ MetaSop │  │  Fact   │  │   Sop   │  │ Session │  │  File  │ │
│  │ (rules) │  │ (facts) │  │ (SOPs)  │  │ +Turn   │  │ +Symbol│ │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘  └────────┘ │
│                                                                  │
│   Indexes on :Fact: vector(768d) + fulltext(BM25)                │
└─────────────────────────────────────────────────────────────────┘
                               ▲
              Aider SQLite hot cache (on Doer host, ephemeral)
                               ▲
                  ┌─────────────────────────┐
                  │  ADK Session (in-mem)   │  ← L1 (per-ticket)
                  └─────────────────────────┘
```

## The five layers

| Layer | Stored in | What lives there | Who reads it | Who writes it |
|---|---|---|---|---|
| **L0** Meta-SOP | Neo4j `:MetaSop` | Procedural rules ("how to write memory") | Learner only | Hand-curated; rare |
| **L1** Working | ADK Session (in-mem on NUC) | Per-ticket scratchpad: turns so far, current state | All agents (current ticket only) | ADK auto-mirrors to L4 |
| **L2** Facts | Neo4j `:Fact` (vector + fulltext) | Distilled wins from past tickets, claude.md / GA memory.md ingests | Doer + Planner system prompts | Learner only, post-pass |
| **L3** SOPs | Neo4j `:Sop` | Task-shape playbooks (e.g. "extract service from controller") | Planner conditional on ticket type | Hand-curated |
| **L4** Sessions | Neo4j `:Session` + `:Turn` | Every agent turn ever fired (auto-remember) | `recall_similar_flows` tool | ADK plugin every turn |
| **L5** Code | Neo4j `:File` + `:Symbol` (+ Aider SQLite hot cache) | AST + call graph + Graphify community structure | Doer system prompt always | Tree-sitter ingest + nightly Graphify |

## What the Doer actually sees per turn (the full stack)

When `agent_runner_loop` builds the prompt, eight sources land in the
context window. In order of injection:

| # | Source | Where from | Size |
|---|--------|------------|------|
| 1 | Ticket body | Postgres `tickets.body` | ~0.5–2 KB |
| 2 | `## Allowed files` | Parsed from ticket body via `parse_allowed_files` | small |
| 3 | **Aider RepoMap digest** | `aiforge_core/memory/code_context.py:aider_digest` — calls `aider.repomap.RepoMap` against the worktree, PageRank-ranked tree-sitter tags | ~1024 tok budget |
| 4 | **Graphify + tree-sitter neighbours** | `code_context.py:graph_neighbours` — Cypher against Neo4j `:File`/`:Symbol`/`:CALLS`/`:IMPORTS`/`:EXTENDS`/`:IMPLEMENTS`/`:DEFINES_METHOD` edges (≤30 lines) | ~1–3 KB |
| 5 | Planner notes (`plan.md`) | `<worktree>/.aiforge/plan.md` | ~1 KB |
| 6 | GA filesystem memory | `~/genericagent/memory/global_mem_insight.txt` + `assets/insight_fixed_structure_en.txt` (read by GA's `get_global_memory()`) | ~3 KB |
| 7 | **Neo4j L2 facts (top-K)** | `aiforge_core/memory/neo4j_facts.py:fetch_facts_text` — fulltext BM25 + 2-hop graph boost, concatenated onto GA filesystem memory by monkey-patch | ~1 KB |
| 8 | Required-workflow checklist | static text in `_build_user_input` | small |

Items 6+7 are fused by the `get_global_memory` monkey-patch — GA reads
filesystem first, then we append Neo4j facts. Both stay live. Item 3
(Aider) is the **hot path** — runs in-process every Doer call. Items
4+7 are Cypher round-trips to NUC Neo4j.

## How each agent uses memory

### Architect (external, Claude Code)
- Reads everything via Cypher; writes nothing.
- Mostly browses L4 sessions to understand prior runs.

### Planner
- **Auto-injected at prompt build**: top-8 L2 facts (hybrid Cypher) +
  any L3 SOP whose `applies_when` matches the ticket labels/title.
- **Tools**: `recall_similar_flows` (queries L4 for prior tickets with
  similar tool sequences), `graph_lookup` (L5 symbol/caller queries).
- **Writes**: nothing. Plan goes to `<worktree>/.aiforge/plan.md`,
  not to memory.

### Doer
- **Auto-injected at prompt build**: top-8 L2 facts scoped to the
  subticket files + Aider RepoMap digest of L5 (PageRank tree-sitter
  signatures, ~1024 tok budget).
- **Tools**: `ask_explorer` (sub-agent for focused L5 exploration
  without context bloat).
- **Writes**: nothing. ScopeGuard rejects writes outside the subticket
  allowlist anyway.

### Feedback
- **Auto-injected**: top-3 L2 facts related to acceptance criteria.
- **Writes**: nothing.

### Learner
- **Auto-injected**: L0 MetaSop (the "how to write memory" rule).
- **Writes**: ONE `:Fact` per pass-verdict ticket via ADK plugin
  (server-side, NOT a tool the model can call). Distills "what worked
  this time" into 1–3 sentences.

## Hybrid retrieval (L2 → top-K)

Single Cypher query fuses three signals:

```cypher
CALL db.index.vector.queryNodes('factEmbedding', 20, $embedding)
YIELD node, score AS s_vec
WITH node, s_vec
CALL db.index.fulltext.queryNodes('factText', $keywords)
YIELD node AS n2, score AS s_lex
WITH node, s_vec, n2, s_lex
WHERE node = n2 OR n2 IS NULL
WITH node, coalesce(s_vec, 0) * 0.7 + coalesce(s_lex, 0) * 0.3 AS sem_score
OPTIONAL MATCH (node)-[:ABOUT]->(:Ticket {id: $ticket_id})
WITH node, sem_score + (CASE WHEN $ticket_id IS NULL THEN 0.0 ELSE 0.2 END) AS final_score
RETURN node, final_score ORDER BY final_score DESC LIMIT 8
```

- 0.7 × vector cosine (semantic match)
- 0.3 × Lucene BM25 (keyword match)
- + 0.2 if the fact is `:ABOUT` the active ticket (graph-hop boost)

Templates live in `aiforge_core/memory/cypher_templates.py`.

## L5 code map (Aider + Graphify + tree-sitter)

Three sources, same Neo4j graph:

| Source | When | What it adds |
|---|---|---|
| **Tree-sitter direct ingest** | One-shot per repo | `:File`, `:Symbol`, `:CALLS`, `:IMPORTS`, `:EXTENDS`, `:IMPLEMENTS`, `:DEFINES` (deterministic AST) |
| **Graphify nightly** | systemd timer | Same shape + INFERRED edges (Claude subagent extracts cross-file semantic edges tree-sitter misses). Tagged `source: 'graphify'` |
| **Aider RepoMap** | Every Doer call | NOT in Neo4j — local SQLite cache + token-budgeted ranked digest injected directly into Doer system prompt |

Stats today (NUC):

| Repo | Files | Symbols | Edges (CALLS) |
|---|---|---|---|
| PosClientBackend | 10,065 | 25,213 | 119,790 |
| oneshell-commons | 518 | seeded | seeded |
| mongoEventListner | 23 | 5,427 | seeded |

## Auto-remember (L4)

Every agent turn writes a `:Turn` node via the ADK
`Neo4jMirrorPlugin.on_event_callback` hook. No agent code changes needed
— it's a global plugin.

```cypher
(:Session {id, ticket_id, agent_role, started_at, ended_at, outcome})
  -[:OF_TICKET]->(:Ticket)
  <-[:IN_SESSION]-(:Turn {n, tool, args_summary, output_summary, ts})
```

Recall: "find similar past sessions that succeeded":

```cypher
MATCH (s:Session {agent_role: $role, outcome: 'pass'})-[:OF_TICKET]->(:Ticket)
WHERE s.id <> $current_session
WITH s, [(s)<-[:IN_SESSION]-(t) | t.tool] AS tool_seq
RETURN s.ticket_id, s.turn_count, s.wall_s, tool_seq
ORDER BY abs(s.turn_count - $expected_turns) ASC
LIMIT 5
```

## Ingestion sources (where facts come from)

| Source | Cadence | Tag |
|---|---|---|
| Learner-distilled facts | per pass-verdict ticket | `source: aiforge_learner` |
| `~/.claude/memory/*.md` | nightly cron on NUC | `source: claude_md` |
| `~/genericagent/memory/*.md` | nightly cron on NUC | `source: ga_l2` / `ga_l3` |
| Manual curation | ad-hoc | `source: manual` |
| GA L4 raw sessions (legacy) | one-shot ingest | `source: ga_l4_legacy` |

All ingestion runs on NUC systemd timers — no MS dependency.

## What memory does NOT do

- **No agent writes its own facts mid-run.** Only the Learner writes,
  only after Feedback says pass. Doer cannot crystallize memory; the
  GA `start_long_term_update` tool is in every agent's forbidden list.
- **No cross-ticket state in L1.** Sessions reset per ticket. Cross-
  ticket continuity comes from L2 facts (semantic), L4 sessions
  (procedural recall), L5 code (structural).
- **No vector search outside L2.** L4 + L5 are graph-traversal only.
  Adding vector indexes elsewhere = scope creep.

## Pointers

- Schema migration: `aiforge_core/memory/schema.py`
- Cypher templates: `aiforge_core/memory/cypher_templates.py`
- Tree-sitter ingest: `aiforge_core/index/treesitter_ingest.py`
- Aider wrapper: `aiforge_core/index/aider_map.py`
- Graphify loader: `aiforge_core/index/graphify_loader.py`
- ADK auto-remember plugin: `aiforge_core/runtime/adk_workflow.py:Neo4jMirrorPlugin`
