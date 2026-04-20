# AIForgeCrew — Autonomous Memory & Tool Orchestration Design

**Status:** Draft for review
**Author:** Manikanta Reddy Pasala
**Date:** 2026-04-21
**Supersedes (partial):** `DESIGN.md` v3 §6 Memory, §7 RAG, §5 Tool Stack
**Applies to:** pipeline v4.1 (Architect / SrDev / Developer / FactExtract)

---

## 1. Goal

Replace the current 2-tier pgmem + single-collection Chroma RAG + broken Hindsight
fact store with a single Postgres-backed memory substrate that supports four
distinct memory tiers, hybrid retrieval with rerank, and a deterministic
orchestrator-driven context assembly layer that is feedback-loop aware.

The design targets **operational stability** and **deterministic debuggability**
first, SOTA autonomy second. It is deliberately one step short of fully
agent-driven memory (AgeMem-style), which requires RL and an eval harness we do
not yet have.

Success criteria:

- A human-written parent ticket completes end-to-end (plan → decompose →
  implement → review → MR) without human intervention, with bounded retries.
- Every agent hop receives a role-specific, budget-capped, citation-tagged
  context bundle assembled by the orchestrator, not by the agent.
- Ticket-trace knowledge is distilled on merge and accumulates across tickets
  without poisoning the memory store.
- Codebase retrieval is AST-aware and returns per-symbol chunks with source
  citations.

Non-goals (this revision):

- Multi-repo federation. Single repo focus.
- Agent-callable memory operations (AgeMem). Deferred until base case is stable.
- FAISS/Weaviate/Qdrant backend. Postgres-only until scale forces a move.
- TDD enforcement. `DESIGN.md` v3 TDD flow is retired under v4.1.

---

## 2. Top-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                HUMAN (Paperclip UI :3100)                   │
│               creates PARENT ticket                         │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ ARCHITECT (Claude Code, external cloud)                     │
│  - rich design comment per parent ticket                    │
│  - ADR + constraints + interface contracts                  │
│  - test expectations + acceptance criteria                  │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ SR DEVELOPER (gemma-4-31b-it, LM Studio, 64K ctx)           │
│  - consumes architect spec                                  │
│  - splits parent → N child tickets                          │
│  - per child: scoped context + insights + edge cases        │
└──────────────────────────────┬──────────────────────────────┘
                               ▼ (one Developer run per child)
┌─────────────────────────────────────────────────────────────┐
│ DEVELOPER (qwen3-coder-next, LM Studio, 128K ctx)           │
│  - diff-aware edits via git_diff tool                       │
│  - runs tests, reports {status, confidence, citations}      │
│  - loops to Architect on review reject (≤3)                 │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ ARCHITECT REVIEW (Claude Code, external)                    │
│  - verify against parent spec                               │
│  - approve → MR on child                                    │
│  - reject → comments back to Developer (≤3 loops)           │
└──────────────────────────────┬──────────────────────────────┘
                               ▼ (after all children merged)
┌─────────────────────────────────────────────────────────────┐
│ FACT EXTRACT (qwen3-4b-thinking, sync post-MR)              │
│  - scans parent + children traces                           │
│  - emits XML: ≤5 facts → T2 proposals, ≤3 recipes → T3      │
│  - human-gated merge; does not write directly               │
└─────────────────────────────────────────────────────────────┘
```

All four roles talk through a single Paperclip parent/child ticket thread.
Ticket comments are the audit surface; they are not used as conversational
context for the next hop. Context is assembled from the memory store and
written as a fresh prompt per invocation.

### 2.1 Runtime hosts

| Component                       | Host        | Port  | Notes                                   |
|---------------------------------|-------------|-------|-----------------------------------------|
| Paperclip UI + API              | Mac Studio  | 3100  | Existing Node runtime                   |
| Paperclip Postgres              | Mac Studio  | 54329 | Issues/comments metadata                |
| LM Studio (inference)           | Mac Studio  | 1234  | gemma-31b, qwen-coder, qwen-4b-thinking |
| pgvector/pg_trgm (memory)       | Mac Studio  | 5432  | New: `aiforge` database                 |
| bge-m3 embedding sidecar        | Mac Studio  | 8764  | FastAPI, ONNX runtime                   |
| bge-reranker-v2-m3 sidecar      | Mac Studio  | 8765  | FastAPI, FlagReranker                   |
| aiforge_core orchestrator       | Laptop      | —     | Python process; talks to Mac Studio     |
| Claude Code Architect           | Laptop CLI  | —     | External cloud invocation               |

### 2.2 VRAM budget (M3 Ultra, 96 GB unified)

| Slot                              | Model                         | VRAM   |
|-----------------------------------|-------------------------------|--------|
| SrDev                             | gemma-4-31b-it Q4_K_M         | ~20 GB |
| Developer                         | qwen3-coder-next (MoE 80B/3B) | ~45 GB |
| Fact Extract                      | qwen3-4b-thinking Q4_K_M      | ~3 GB  |
| Embed sidecar                     | bge-m3 ONNX                   | ~2 GB  |
| Rerank sidecar                    | bge-reranker-v2-m3            | ~1 GB  |
| **Total hot**                     |                               | **~71 GB** |
| Headroom                          |                               | ~25 GB |

All four agent models stay resident. Architect runs off-host (Claude cloud),
so no local cost.

---

## 3. Memory Tiers

Four tiers live in one Postgres 17 database (`aiforge`) with extensions
`vector` (pgvector) and `pg_trgm` (trigram/BM25-ish).

### 3.1 Tier inventory

| Tier | Wing pattern         | Purpose                                          | TTL / retention          | Writer                    | Reader      |
|------|----------------------|--------------------------------------------------|--------------------------|---------------------------|-------------|
| T1   | `ticket/<parent-id>` | Per-ticket episodic trace: tool calls, decisions | Delete 7 days post-merge | All agents (direct)       | All agents  |
| T2   | `project`            | Distilled cross-ticket semantic facts            | Indefinite               | Reflection runner (gated) | All agents  |
| T3   | `skills`             | Procedural recipes / how-to / tool patterns      | Indefinite               | Reflection runner (gated) | All agents  |
| T4   | `code/<repo>`        | AST-chunked source + docs                        | Rebuilt on push to main  | Reindex CLI (automated)   | All agents  |

### 3.2 Schema

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE memories (
    id          BIGSERIAL PRIMARY KEY,
    tier        TEXT NOT NULL CHECK (tier IN ('t1','t2','t3','t4')),
    wing        TEXT NOT NULL,            -- e.g. ticket/ONE-77, project, skills, code/aiforge
    parent_id   TEXT,                     -- ticket-id for T1, repo for T4, null else
    kind        TEXT NOT NULL,            -- 'tool_call' | 'decision' | 'fact' | 'recipe' | 'chunk' | ...
    source      TEXT,                     -- file path, ticket comment id, etc
    title       TEXT,
    text        TEXT NOT NULL,
    embedding   vector(1024),             -- bge-m3 dim
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ                -- set for T1 only, null = indefinite
);

CREATE INDEX idx_memories_tier_wing  ON memories(tier, wing);
CREATE INDEX idx_memories_parent     ON memories(parent_id);
CREATE INDEX idx_memories_expires    ON memories(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX idx_memories_embedding  ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_memories_text_trgm  ON memories USING gin (text gin_trgm_ops);
CREATE INDEX idx_memories_title_trgm ON memories USING gin (title gin_trgm_ops);
```

### 3.3 Write gating

- **T1 (episodic)**: any agent writes via `append_event()` tool. High volume
  expected (every tool call emits one row). Insert only, no edits.
- **T2 (semantic) + T3 (procedural)**: only the reflection runner writes.
  Reflection output is queued in `memory_proposals` table, requires human
  approval before merge into `memories`. Agents never write T2/T3 directly.
- **T4 (codebase)**: only the reindex CLI writes. Triggered by git push hook
  on main, or manually via `make rag-reindex`.

```sql
CREATE TABLE memory_proposals (
    id           BIGSERIAL PRIMARY KEY,
    tier         TEXT NOT NULL CHECK (tier IN ('t2','t3')),
    wing         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    title        TEXT,
    text         TEXT NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_trace TEXT NOT NULL,           -- parent ticket id
    proposed_by  TEXT NOT NULL,           -- 'fact_extract'
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ,
    decided_by   TEXT
);
```

### 3.4 TTL enforcement

A nightly cron (via existing launchd agent infra) runs:

```sql
DELETE FROM memories
WHERE tier = 't1'
  AND expires_at IS NOT NULL
  AND expires_at < now();
```

T1 rows get `expires_at = merged_at + interval '7 days'` at the moment the
parent ticket transitions to `merged`.

### 3.5 Embedding

Single model, single vector space: **BAAI/bge-m3** (1024-dim dense), served
locally from an ONNX FastAPI sidecar. All four tiers embed through the same
endpoint. No dual-space risk.

LM Studio's nomic-embed-text and Hindsight's embedder are both retired. The
codebase-wide `embed()` helper lives in `aiforge_core.embed` and talks only to
the sidecar.

---

## 4. Retrieval Stack

Hybrid search → fusion → rerank. Per-role retrieval policy selects weights.

### 4.1 Pipeline

```
 query text
     │
     ├── BM25-ish (pg_trgm similarity)   ── top 50 by title+text similarity
     │
     └── vector (pgvector HNSW cosine)   ── top 50 by embedding
                     │
                     ▼
       Reciprocal Rank Fusion (k=60)     ── merge to top N_fuse candidates
                     │                        (N_fuse = sum of per-tier top_k
                     │                         per role, typically 30–50)
                     ▼
       bge-reranker-v2-m3 cross-encoder  ── rescore, return top N_keep
                     │
                     ▼
       Budget-aware packer               ── pack until token cap per field
```

### 4.2 Per-role retrieval policy

| Role        | Tier priority               | top_k (per tier) | Rerank keep |
|-------------|-----------------------------|------------------|-------------|
| Architect   | T2 → T4 → T3 → T1           | 8 / 8 / 4 / 8    | top 10      |
| SrDev       | T2 → T3 → T4 → T1           | 6 / 8 / 12 / 8   | top 12      |
| Developer   | T4 → T3 → T1 → T2           | 20 / 6 / 8 / 4   | top 15      |
| Fact Extract| T1 (of the closing parent)  | 200              | top 50      |

Tier filtering is expressed as a SQL predicate, not a runtime filter on
results. That guarantees we don't waste the reranker on irrelevant tiers.

### 4.3 Rerank sidecar contract

```
POST http://mac-studio:8765/rerank
{
  "query": "how does push sync publish to NATS",
  "candidates": [
    {"id": "mem:12345", "text": "..."},
    ...
  ]
}
→ 200
{
  "scores": [0.87, 0.71, ...],
  "order":  [2, 0, 4, 1, 3, ...]
}
```

FP16 on MPS when available, FP32 CPU fallback. Batch size ≤32. Wall target:
≤150 ms for 30 pairs.

---

## 5. Context Assembly & Compaction

This is the layer the agents see. The orchestrator builds a prompt per hop.
Agents receive text only; they do not choose what to retrieve.

### 5.1 Prompt shape (per hop)

```
[system]            role system prompt from agents/<role>/system-prompt.md
[task]              current ticket body + acceptance criteria
[retrieved code]    T4 chunks, AST-scoped, full text, NEVER compressed
[retrieved memory]  T2/T3/T1 hits, cited
[recent work]       summarized prior-hop results + structured tool outputs
[tools]             JSON schemas of tools available to this role
[output contract]   required output shape, including `confidence: 0-1`
```

### 5.2 Compaction rules (hard)

1. **Never compress** the current task body, retrieved code chunks, or tool
   output that is being acted on in the current hop.
2. **Only compress** prior-hop transcripts, ticket comments older than the
   last state transition, and superseded intermediate artefacts.
3. Compaction is deterministic and orchestrator-driven, not agent-driven. If
   prior-hop transcripts exceed 4 KB, they are replaced with a bulleted
   summary produced by the qwen3-4b-thinking model via a fixed prompt.
4. Compaction output includes IDs that still point to the uncompressed T1
   rows, so the agent can re-retrieve on demand via `search_memory`.

### 5.3 Budgets

Per role, hard caps enforced before send. If the assembled prompt exceeds
the cap, the orchestrator drops lowest-ranked retrieval hits until it fits.

| Role        | Prompt cap | Output cap | Rationale                        |
|-------------|------------|------------|----------------------------------|
| Architect   | 80 KB      | 8 KB       | Claude cloud; room for full spec |
| SrDev       | 48 KB      | 4 KB       | gemma 64K ctx                    |
| Developer   | 90 KB      | 12 KB      | qwen-coder 128K ctx              |
| Fact Extract| 60 KB      | 2 KB       | XML out only                     |

### 5.4 Diff-aware editing

Developer edits use a fixed patch protocol, not full-file rewrites. The
`write_file` tool accepts either `full` mode or `patch` mode (unified diff).
Default is `patch`. `full` requires justification in the tool call.

---

## 6. Tool Surface

All tools exposed as MCP JSON-schema endpoints. All tools return structured
JSON with `status`, `result`, optional `error`, and `citations`. Per-role
permission matrix gates which tools each role may call.

### 6.1 Tool inventory

| Tool            | Input summary                          | Output summary                           |
|-----------------|----------------------------------------|------------------------------------------|
| `search_memory` | `{q, tiers[], top_k}`                  | `{hits[{id, tier, text, source, score}]}`|
| `search_code`   | `{q, repo, top_k, symbol_kind?}`       | `{hits[{path, symbol, text, score}]}`    |
| `read_file`     | `{path, start_line?, end_line?}`       | `{path, content, lines}`                 |
| `write_file`    | `{path, mode, patch? \| content?}`     | `{status, path, applied_bytes}`          |
| `git_diff`      | `{ref?, paths?}`                       | `{diff, files_changed[]}`                |
| `git_ops`       | `{op, args}` (branch/commit/push)      | `{status, ref, message?}`                |
| `run_tests`     | `{paths[]?, filter?}`                  | `{passed, failed, total, report_path}`   |
| `run_command`   | `{cmd, cwd?, timeout_s?}`              | `{status, stdout, stderr, exit_code}`    |
| `report`        | `{status, summary, confidence}`        | `{ack: true}`                            |
| `append_event`  | `{kind, title, text, metadata?}`       | `{id}` — writes T1                       |

### 6.2 Per-role permission matrix

| Tool          | Architect | SrDev | Developer | Fact Extract |
|---------------|:---------:|:-----:|:---------:|:------------:|
| search_memory |     ✅    |  ✅   |    ✅     |      ✅      |
| search_code   |     ✅    |  ✅   |    ✅     |      ❌      |
| read_file     |     ✅    |  ✅   |    ✅     |      ❌      |
| write_file    |     ❌    |  ❌   |    ✅     |      ❌      |
| git_diff      |     ✅    |  ✅   |    ✅     |      ❌      |
| git_ops       |     ❌    |  ❌   |    ✅     |      ❌      |
| run_tests     |     ❌    |  ❌   |    ✅     |      ❌      |
| run_command   |     ❌    |  ❌   |    ✅*    |      ❌      |
| report        |     ✅    |  ✅   |    ✅     |      ✅      |
| append_event  |     ✅    |  ✅   |    ✅     |      ✅      |

`*` `run_command` is scoped to an allowlist (build/test runners; no network).

### 6.3 Structured report contract

Every agent's terminal turn emits a `report` tool call:

```json
{
  "status": "done" | "needs_more_context" | "failed",
  "summary": "short human-readable summary",
  "confidence": 0.0,
  "next_action": "review" | "retry" | "escalate",
  "citations": ["mem:12345", "code:src/foo.py#symbol"]
}
```

Confidence routes:

- `>= 0.7` → proceed to next lifecycle transition
- `0.5 – 0.7` → re-invoke same role with expanded context (retry ≤3)
- `< 0.5` → escalate to human via ticket comment + Paperclip assignee=human

---

## 7. Lifecycle v4.1

Supersedes `DESIGN.md` v3 §4 TDD lifecycle. Parent tickets split into children;
all roles comment on the ticket thread they are assigned to.

### 7.1 State machine

```
PARENT ticket states:
  created
     → planning(Architect)
     → splitting(SrDev)
     → spawned        (children exist, parent waits)
     → reflection(FactExtract)   — once all children merged
     → closed

CHILD ticket states (one per Developer task):
  created(by SrDev)
     → coding(Developer)
     → reviewing(Architect)
         ├── approve → mr_created → merged
         └── reject  → coding (≤3 loops)
     → escalated     — loop cap hit or breaker tripped
```

Transitions are enforced by `aiforge_core.lifecycle.advance()`, re-used from
the existing module. The transition table is replaced to match the new SM.
Coverage-gate enforcement from `retry.py` is retained.

### 7.2 Transition routing

| From state       | To state      | Routes to     |
|------------------|---------------|---------------|
| created (parent) | planning      | architect     |
| planning         | splitting     | sr_developer  |
| splitting        | spawned       | — (parent waits) |
| all_children_merged | reflection | fact_extract  |
| reflection       | closed        | — (terminal)  |
| created (child)  | coding        | developer     |
| coding           | reviewing     | architect     |
| reviewing        | mr_created    | — (human merge) |
| reviewing        | coding        | developer (reject loop) |
| mr_created       | merged        | — (human action) |

---

## 8. Reflection & Consolidation

Runs once per parent on full-merge. Sync, not async, because qwen3-4b-thinking
is small enough to stay loaded alongside the other roles.

### 8.1 Inputs

All supplied by the orchestrator in the assembled prompt; Fact Extract does
not need tool access to gather them.

- Full parent ticket body + all child bodies
- All T1 rows for `parent_id = <parent-id>`
- Diff summary across all merged children (precomputed by orchestrator via
  `git diff <base>..<merge-head>` on each child branch and stitched together)

### 8.2 Output (XML)

The reflection prompt constrains the model to emit:

```xml
<reflection>
  <facts>
    <fact kind="convention">Text of fact, ≤300 chars.</fact>
    <fact kind="constraint">...</fact>
  </facts>
  <recipes>
    <recipe title="Short name">
      <when>When to apply this recipe.</when>
      <how>Concrete steps, ≤500 chars.</how>
    </recipe>
  </recipes>
</reflection>
```

Max 5 facts, max 3 recipes per ticket. Soft failures (empty output, malformed
XML) are logged and skipped — the reflection run doesn't block ticket
closure.

### 8.3 Human gate

All reflection output goes into `memory_proposals`. A CLI surface
(`make reflection-review` / `aiforge propose list|approve|reject <id>`)
lets the human skim and accept into T2/T3. Nothing lands in `memories` without
explicit approval.

---

## 9. Failure Control

Explicit, enforced in code, not in agent prompts.

| Control                  | Value              | Enforcement                 |
|--------------------------|--------------------|-----------------------------|
| Max steps per ticket     | 20 tool calls      | orchestrator counter        |
| Max retries per step     | 3                  | `retry.py` breaker          |
| Tool timeout             | 60 s (default)     | subprocess / httpx timeout  |
| LLM request timeout      | 300 s              | LM Studio client timeout    |
| Review reject loop cap   | 3                  | `enforce_loop_caps()`       |
| Global kill switch       | `.aiforge/KILL`    | checked before every hop    |
| Per-ticket kill          | ticket tag `kill`  | checked before every hop    |
| Breaker threshold        | 3 consecutive fail | `CircuitBreaker`            |
| Confidence escalate      | < 0.3              | orchestrator routes to human|

All of these exist partially in `retry.py` and `lifecycle.py` today. This
design formalizes the thresholds and adds the missing kill-switch file check
and `confidence` routing.

---

## 10. Testing Strategy

Three layers.

### 10.1 Unit

- Store schema round-trips (insert, embed, query, TTL).
- Retrieval stack components (BM25 only, vector only, RRF fusion, rerank)
  with fixture memory rows.
- Context assembler: given a synthetic role + task + memory set, assert the
  assembled prompt respects caps, never compresses code, orders tiers
  correctly.
- Lifecycle SM transitions + loop caps + coverage gate.

### 10.2 Integration (no LLM)

- Swap each agent role with a deterministic `replay` fake that reads a
  canned tool-call sequence from a fixture file.
- Drive a full parent-ticket-to-merge flow, assert T1 population, reflection
  runner produces proposals, memory-gated writes don't land in T2/T3 without
  approval.
- Kill switch + breaker + retry counters all covered by this layer.

### 10.3 Smoke (live LLM)

- One goldens ticket per agent role, run against LM Studio on Mac Studio,
  assert report shape + confidence distribution + tool call counts within
  expected band.
- Skipped by default in CI, runnable via `make smoke`.

---

## 11. Migration Path

Existing code touched, file by file:

| File                                 | Change                                     |
|--------------------------------------|--------------------------------------------|
| `aiforge_core/pgmem.py`              | Replace: new schema, tier column, single embed endpoint. Drop 2-tier ACL. |
| `aiforge_core/rag.py`                | Replace: pgvector backend, AST chunking via tree-sitter, single embed. Remove Chroma. |
| `aiforge_core/lifecycle.py`          | Replace transition table to v4.1 (parent/child). Keep `advance()`, `allowed_next_states()`. |
| `aiforge_core/config.py`             | Update `Routing` dataclass to 4-role v4.1 fields. |
| `aiforge_core/retry.py`              | Keep; retune thresholds; add kill-switch file check. |
| `aiforge_core/observe.py`            | Add per-role `confidence` and `memory_tier_hits` counters. |
| `aiforge_core/embed.py` (new)        | Single `embed()` helper, talks to :8764 sidecar. |
| `aiforge_core/retrieval.py` (new)    | Hybrid pipeline: BM25, vector, RRF, rerank, packer. |
| `aiforge_core/context.py` (new)      | Prompt assembler + compactor. |
| `aiforge_core/reflection.py` (new)   | Fact Extract runner + `memory_proposals` CLI. |
| `agents/*/system-prompt.md`          | Rewrite: list tools, output contract, confidence requirement. |
| `agents/fact-extract/` (new)         | New role dir: system prompt + permissions. |
| `mcp/rag-server.json`                | Replace `query` → `search_memory` + `search_code`. |
| `mcp/memory-server.json` (new)       | New: `append_event`, `search_memory`. |
| `paperclip.config.yml`               | Update routing + org chart to v4.1 four roles. |
| `scripts/install-embed-sidecar.sh` (new) | bge-m3 FastAPI sidecar installer. |
| `scripts/install-rerank-sidecar.sh` (new) | bge-reranker-v2-m3 FastAPI installer. |
| `scripts/hermes-seed-memory.sh`      | Delete (Hindsight retirement). |
| `scripts/hermes-setup-hindsight.sh`  | Delete. |
| `scripts/patch-hindsight-shutdown-bug.sh` | Delete. |
| `DESIGN.md`                          | Mark §4, §5, §6, §7 superseded; link here. |

Migration order:

1. Stand up Postgres schema + sidecars (embed, rerank). No agent changes yet.
2. Reindex T4 (codebase) with AST chunking; backfill T2 from existing
   curated facts; leave T1 + T3 empty.
3. Swap `rag.py` and `pgmem.py` to new module. Run existing test suite; fix
   regressions.
4. Rewrite lifecycle + routing to v4.1. Add fact-extract role.
5. Rewrite agent system prompts; wire MCP servers.
6. Run integration smoke ticket end-to-end on a throwaway parent.
7. Delete Hindsight scripts.

---

## 12. Decision Log

| ID | Decision                                  | Alt considered                  | Why                                                |
|----|-------------------------------------------|---------------------------------|----------------------------------------------------|
| D1 | Orchestrator-driven context assembly      | AgeMem agent-driven memory ops  | AgeMem needs RL + eval rig we don't have           |
| D2 | Single store (Postgres + pgvector)        | Dual store (pg + Chroma)        | Ops simplicity; existing pgvector install          |
| D3 | Single embed model (bge-m3 1024d)         | nomic + bge fallback            | Embedding-space consistency; LM Studio SPOF removed|
| D4 | 4 sync roles, all models resident         | Async Fact Extract              | qwen3-4b-thinking fits budget (3 GB)               |
| D5 | Sub-tickets (parent/child)                | Single ticket per v4            | User-requested; matches Architect→SrDev decomposition |
| D6 | Reflection is human-gated                 | Auto-write to T2/T3             | Prevent memory poisoning                           |
| D7 | Diff-aware editing                        | Full-file rewrite               | Token cost + accuracy                              |
| D8 | Keep pgvector until scale pressure        | FAISS upfront                   | Avoid premature optimization; escape hatch defined |
| D9 | Drop Hindsight entirely                   | Fix Hindsight                   | LM Studio json_object rejection + NIM 429 + small value |
| D10| TDD / Tester role removed                 | Keep Tester                     | Matches committed v4 direction; Developer writes tests |

---

## 13. Open Questions

- Should T4 codebase index cover external referenced repos
  (e.g. `~/codeRepo/PosPythonBackend`) or stay AIForgeCrew-only? Memory note
  `project_one48_boi_v2` suggests external repos are in scope.
- Confidence calibration: local models tend to report overconfident. Need a
  calibration pass after first 10 tickets; may require per-role offset.
- Reflection of failed tickets: do we reflect on escalated tickets too, to
  capture anti-patterns? Proposed yes, kind=`anti_pattern`.
- Architect-as-Claude-Code authentication: how is it invoked
  non-interactively from the orchestrator? Current
  `scripts/paperclip-em-use-claude.sh` uses an interactive shell on Mac
  Studio. Needs a headless mode.
