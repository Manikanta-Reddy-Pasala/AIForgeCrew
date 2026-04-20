# AIForgeCrew

**Autonomous AI dev team.** Human files a ticket in Paperclip → 4 AI agents plan, decompose, implement, review, and open a PR. Single parent-ticket thread, sub-tickets per work unit, cross-session memory, code knowledge graph, hybrid retrieval. Runs on one Mac Studio (M3 Ultra, 96 GB). Laptop = remote control.

**Pipeline:** v4.1 (2026-04-21)
**Spec:** [`docs/superpowers/specs/2026-04-21-autonomous-memory-orchestration-design.md`](./docs/superpowers/specs/2026-04-21-autonomous-memory-orchestration-design.md)
**Plan:** [`docs/superpowers/plans/2026-04-21-memory-orchestration.md`](./docs/superpowers/plans/2026-04-21-memory-orchestration.md)
**Runbook:** [`docs/runbook.md`](./docs/runbook.md)

---

## TL;DR — first ticket in 10 minutes

```bash
# Mac Studio (once)
bash scripts/install-pg-aiforge.sh              # Postgres schema
bash scripts/install-embed-sidecar.sh &         # bge-m3 embed on :8764
bash scripts/install-rerank-sidecar.sh &        # bge-reranker on :8765
bash scripts/install-graphify.sh                # code knowledge graph
.venv/bin/aiforge memory reindex-code --repo aiforge --root .
bash scripts/install-sidecar-agents.sh install  # launchd auto-start
bash scripts/paperclip-bootstrap-v41.sh         # 4 v4.1 agents + prompts

# Fire a ticket — assign to Architect agent in Paperclip UI or via SQL.
# Heartbeat picks it up within 60 s. Watch comments flow on the parent.
```

---

## How it works

### 1. Pipeline — 4 agents, 1 parent ticket, N children

```
 HUMAN ──► Paperclip ticket (parent)
                │
                ▼
 ┌──────────── ARCHITECT ─────────────┐
 │ Claude Opus 4.7 OR gemma-4-31b-it  │   designs: spec, ADR, contracts,
 │ (AIFORGE_ARCHITECT_MODE toggle)    │   acceptance criteria, test plan
 └─────────────────┬──────────────────┘
                   ▼
 ┌────────────── SR DEVELOPER ────────┐
 │ gemma-4-31b-it @ 64K               │   splits parent → N child tickets
 └─────────────────┬──────────────────┘   each w/ scoped context + insights
                   │
        (one Developer run per child)
                   ▼
 ┌────────────── DEVELOPER ───────────┐
 │ qwen3-coder-next MoE 80B/3B @ 128K │   writes code + tests, diff-patch,
 └─────────────────┬──────────────────┘   commits, pushes, opens PR
                   ▼
 ┌──────── ARCHITECT REVIEW ──────────┐
 │ same as Architect above            │   approve → MR;  reject → loop ≤3
 └─────────────────┬──────────────────┘
                   ▼ (after every child merged)
 ┌──────── FACT EXTRACT ──────────────┐
 │ qwen3-4b-thinking-2507             │   distils facts + recipes from
 └─────────────────┬──────────────────┘   ticket trace → T2/T3 proposals
                   ▼
            human-gated memory merge
```

| Role | Model | Context | Writes | Reads |
|------|-------|---------|--------|-------|
| Architect | claude-opus-4-7 *or* gemma-4-31b-it | 200K / 64K | parent comment only | T2 · T4 · T3 · T1 · graph |
| Sr Developer | gemma-4-31b-it | 64K | child tickets | T2 · T3 · T4 · T1 · graph |
| Developer | qwen3-coder-next | 128K | src + tests + commits | T4 · T3 · T1 · T2 · graph |
| Fact Extract | qwen3-4b-thinking-2507 | 32K | T2/T3 *proposals* only | T1 of one parent |

### 2. Lifecycle state machine (v4.1)

**Parent ticket:**

```
  created → planning(Arch) → splitting(SrDev) → spawned
                                                    │
                               all children merged  ▼
                                          reflection(FactExtract) → closed
```

**Child ticket** (one per Developer task):

```
  created(by SrDev) → coding(Dev) → reviewing(Arch)
                                        │
                                        ├─ approve → mr_created → merged
                                        └─ reject  → coding (loop ≤3)
                                                   → escalated (cap hit)
```

Enforced in `aiforge_core/lifecycle.py`. Invalid transitions raise `LifecycleError`.

---

## Memory system — 4 tiers + graph

> RAG = **execution layer** (give me the code chunk)
> Graphify = **understanding layer** (why is it shaped this way, what depends on it)

```
┌───────────────────────────────────────────────────────────────┐
│                 POSTGRES 17 — aiforge DB                      │
│              pgvector (HNSW) + pg_trgm (BM25-ish)             │
├───────────────────────────────────────────────────────────────┤
│  T1 EPISODIC   per-ticket trace, tool calls, decisions        │
│                wing = ticket/<parent-id>                      │
│                TTL: 7 days after parent merges                │
│                writer: any agent (append-only)                │
│                                                               │
│  T2 SEMANTIC   distilled cross-ticket facts, conventions      │
│                wing = project                                 │
│                writer: reflection runner (human-gated)        │
│                                                               │
│  T3 PROCEDURAL recipes, how-to, tool-use patterns             │
│                wing = skills                                  │
│                writer: reflection runner (human-gated)        │
│                                                               │
│  T4 CODEBASE   AST-chunked source + docs, per symbol          │
│                wing = code/<repo>                             │
│                rebuilt: post-commit hook + reindex CLI        │
└───────────────────────────────────────────────────────────────┘
                  ▲                 ▲
                  │                 │
    ┌─────────────┴──┐    ┌─────────┴────────────┐
    │ bge-m3 ONNX     │   │ memory_proposals     │
    │ embed :8764     │   │ (pending human OK    │
    │ 1024-d dense    │   │  before T2/T3 write) │
    └─────────────────┘   └──────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│        GRAPHIFY — code knowledge graph (understanding)        │
├───────────────────────────────────────────────────────────────┤
│  tree-sitter AST → NetworkX → Leiden clustering               │
│  graphify-out/graph.json       (persisted)                    │
│  graphify-out/GRAPH_REPORT.md  (top insights)                 │
│  graphify-out/graph.html       (interactive)                  │
│  current: 424 nodes, 864 edges, 26 communities                │
│                                                               │
│  Agents call `search_graph {mode=query|path|explain}` via MCP │
│  mode=explain  → "why is this designed like this?"            │
│  mode=path     → dependency chain from A to B (blast radius)  │
│  mode=query    → general graph search                         │
└───────────────────────────────────────────────────────────────┘
```

**Why 4 tiers, not 1:** different write gates + TTLs. T1 high-volume ephemeral, T4 fully automated, T2/T3 curated knowledge that only lands after human review so the memory never poisons itself.

### Retrieval pipeline

```
 query text
    │
    ├── BM25 (pg_trgm similarity on text + title)   → top-K per tier
    │
    └── vector (pgvector HNSW cosine over bge-m3)   → top-K per tier
                   │
                   ▼
         Reciprocal Rank Fusion (k=60)  — merge all ranked lists
                   │
                   ▼
         bge-reranker-v2-m3 cross-encoder (:8765, FP16)
                   │
                   ▼
         Budget-aware packer (drop lowest if over token cap)
                   │
                   ▼
            assembled prompt w/ citations
```

Per-role retrieval policy (`aiforge_core/retrieval.py::ROLE_POLICIES`) sets tier priority + top_k per role. Example — Developer:

```python
tiers = [
    {tier: t4, top_k: 20, wing_prefix: "code/"},   # codebase first
    {tier: t3, top_k: 6,  wing_prefix: "skills"},  # recipes
    {tier: t1, top_k: 8},                           # this ticket's history
    {tier: t2, top_k: 4},                           # cross-cutting facts
]
rerank_keep = 15
```

### Context assembly rules (hard)

Enforced in `aiforge_core/context.py::assemble_prompt`:

1. **Never compress** current task body, retrieved code chunks, tool schemas, output contract.
2. **Only compress** prior-hop transcripts (into ≤5 bulleted summary via qwen3-4b-thinking).
3. Over budget → drop lowest-ranked **memory** hits first. Never the code.
4. Every section is cited (`[mem:12345]`, `[code:src/foo.py#symbol]`).

---

## Tools + MCP surface

All tools exposed via MCP JSON schemas. All return structured JSON with `status`, `result`, `error?`, `citations[]`.

| Tool | Purpose |
|------|---------|
| `search_memory` | hybrid retrieval via role policy |
| `search_code` | AST-chunked T4 retrieval |
| `search_graph` | Graphify code KG (query / path / explain) |
| `read_file` | read with optional line range |
| `write_file` | unified-diff patch (default) or full-file rewrite |
| `git_diff` | working tree or between-refs diff |
| `git_ops` | branch / commit / push (Developer only) |
| `run_tests` | pytest runner, pass/fail + report path |
| `run_command` | allowlisted build/test cmd |
| `report` | agent's terminal turn — status + summary + confidence + citations |
| `append_event` | write a T1 episodic row |

### Permissions matrix

|                 | Architect | SrDev | Developer | FactExtract |
|-----------------|:---------:|:-----:|:---------:|:-----------:|
| search_memory   | ✅ | ✅ | ✅ | ✅ |
| search_code     | ✅ | ✅ | ✅ | ❌ |
| search_graph    | ✅ | ✅ | ✅ | ❌ |
| read_file       | ✅ | ✅ | ✅ | ❌ |
| write_file      | ❌ | ❌ | ✅ | ❌ |
| git_diff        | ✅ | ✅ | ✅ | ❌ |
| git_ops         | ❌ | ❌ | ✅ | ❌ |
| run_tests       | ❌ | ❌ | ✅ | ❌ |
| run_command     | ❌ | ❌ | ✅* | ❌ |
| report          | ✅ | ✅ | ✅ | ✅ |
| append_event    | ✅ | ✅ | ✅ | ✅ |

`*` Developer `run_command` is allowlisted — only build / test runners, no network.

### `report` contract

Every agent's last turn emits:

```json
{
  "status": "done" | "needs_more_context" | "failed",
  "summary": "short human-readable line",
  "confidence": 0.0,
  "next_action": "review" | "retry" | "escalate",
  "citations": ["mem:12345", "code:src/foo.py#symbol"]
}
```

Orchestrator routes on `confidence`:

| Range | Action |
|-------|--------|
| ≥ 0.70 | proceed to next lifecycle state |
| 0.30 – 0.70 | re-invoke same role with expanded context (retry ≤3) |
| < 0.30 | escalate — reassign to human, ticket tag `blocked` |

---

## Failure control

Hard, code-enforced limits (`aiforge_core/retry.py`, `aiforge_core/lifecycle.py`).

| Guard | Value | Where |
|-------|-------|-------|
| Max steps per ticket | 20 tool calls | orchestrator counter |
| Max retries per step | 3 | `CircuitBreaker` |
| Tool timeout | 60 s | subprocess / httpx |
| LLM request timeout | 300 s | LM Studio client |
| Review reject cap | 3 | `enforce_loop_caps()` |
| Confidence escalate | < 0.30 | `confidence_route()` |
| Stale ticket timeout | 120 min | `fleet_summary()` |
| Global kill switch | `.aiforge/KILL` file | `kill_switch_tripped()` |
| Per-ticket kill | label `kill` | same |

### Kill switch — when it fires, how to trip it

The orchestrator calls `kill_switch_tripped(...)` **before every agent hop**. Either signal stops the pipeline for the affected scope.

**Global (halts every ticket, every agent):**

```bash
ssh manikanta@192.168.70.185 'touch ~/AIForgeCrew/.aiforge/KILL'
# release
ssh manikanta@192.168.70.185 'rm ~/AIForgeCrew/.aiforge/KILL'
```

Use global when:
- Paperclip is mis-routing tickets
- A model started looping / burning budget
- You need to apply a config change without racing live agents

**Per-ticket (stops one ticket):** apply the red `kill` label in Paperclip UI, or:

```bash
KILL_LABEL_ID=d2e52007-ae22-4448-b952-f6176ee32e9c
TICKET_UUID=<issue-uuid>
ssh manikanta@192.168.70.185 "curl -s -X POST \
  http://localhost:3100/api/issues/$TICKET_UUID/labels \
  -H 'Content-Type: application/json' \
  -d '{\"labelId\":\"$KILL_LABEL_ID\"}'"
```

Use per-ticket when:
- One ticket is in a loop but others should keep running
- A specific ticket leaked bad context and you want to force human review

The orchestrator treats either signal as a **graceful stop**: current in-flight tool call finishes, agent's last `report` is recorded, ticket moves to `escalated` state. No partial commits pushed.

---

## Runtime hosts + ports

All local. No cloud deps except Architect-in-cloud-mode.

| Component | Host | Port | Notes |
|-----------|------|------|-------|
| Paperclip UI + API | Mac Studio | 3100 | Node + React |
| Paperclip embedded Postgres | Mac Studio | 54329 | paperclip/paperclip |
| LM Studio inference | Mac Studio | 1234 | gemma-4-31b, qwen-coder, qwen-4b-thinking |
| aiforge Postgres | Mac Studio | 5432 | pgvector + pg_trgm, manikanta |
| bge-m3 embed sidecar | Mac Studio | 8764 | FastAPI + ONNX + CoreML |
| bge-reranker-v2-m3 sidecar | Mac Studio | 8765 | FastAPI + FlagReranker FP16 |
| Graphify MCP | Mac Studio | stdio | via `search_graph` tool |
| aiforge_core orchestrator | Laptop | — | Python, talks to Mac Studio |
| Claude Code | Laptop | — | Architect (cloud mode only) |

### VRAM budget (M3 Ultra 96 GB unified)

| Slot | Model | VRAM |
|------|-------|------|
| Architect | claude cloud (`cloud` mode) OR gemma-4-31b-it (`local_30b`) | 0 / 20 GB |
| Sr Developer | gemma-4-31b-it (shared w/ Arch in local_30b) | 20 / 0 GB |
| Developer | qwen3-coder-next (MoE 80B, 3B active) | 45 GB |
| Fact Extract | qwen3-4b-thinking-2507 | 3 GB |
| Embed sidecar | bge-m3 ONNX | 2 GB |
| Rerank sidecar | bge-reranker-v2-m3 | 1 GB |
| **Total (cloud Arch)** | | **~71 GB** (25 GB headroom) |
| **Total (local_30b)** | | **~71 GB** (Arch+SrDev share 20 GB instance) |

---

## 30B-only mode (no Claude cloud)

Flip Architect to local gemma:

```bash
ssh manikanta@192.168.70.185 'cd AIForgeCrew && bash scripts/flip-architect-mode.sh local_30b'
```

Flip back:

```bash
ssh manikanta@192.168.70.185 'cd AIForgeCrew && bash scripts/flip-architect-mode.sh cloud'
```

The local_30b variant uses a simplified prompt (`agents/architect/system-prompt.local-30b.md`) — strict 5-section output (Problem / Plan / Interfaces / Acceptance / Tests) tuned for a 31B dense model.

---

## Daily ops

```bash
# Rebuild code KG after edits
make graphify-rebuild

# Reindex T4 after significant edits (post-commit hook does this auto)
.venv/bin/aiforge memory reindex-code --repo aiforge --root .

# Review + approve semantic/procedural proposals from reflection
.venv/bin/aiforge memory propose-list
.venv/bin/aiforge memory propose-approve <id>
.venv/bin/aiforge memory propose-reject  <id>

# GC expired T1 rows (post-merge, 7-day TTL)
python3 -c "from aiforge_core.store_v2 import Store; print('gc:', Store().gc_expired())"

# Sidecar LaunchAgents
make sidecar-agents-status
make sidecar-agents-restart
make sidecar-agents-uninstall
```

### Automatic refresh on every commit

Installed post-commit hook (`.git/hooks/post-commit`):
- Runs `graphify update .` in the background
- Runs `aiforge memory reindex-code` in the background
- Non-blocking — commit finishes immediately, refresh completes asynchronously

Install on any clone:

```bash
bash scripts/install-post-commit-hook.sh
```

---

## Fresh Mac Studio bring-up

```bash
# Repo
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew ~/AIForgeCrew
cd ~/AIForgeCrew

# Dependencies (homebrew)
brew install postgresql@16 pgvector uv

# pgvector for pg16 (homebrew pgvector targets latest pg; rebuild against @16)
cd /tmp && git clone --branch v0.8.2 https://github.com/pgvector/pgvector.git
cd pgvector
PG_CONFIG=/opt/homebrew/opt/postgresql@16/bin/pg_config make
PG_CONFIG=/opt/homebrew/opt/postgresql@16/bin/pg_config make install
brew services start postgresql@16

# aiforge DB schema
cd ~/AIForgeCrew && bash scripts/install-pg-aiforge.sh

# Sidecars (downloads models ~3 GB total)
bash scripts/install-embed-sidecar.sh &
bash scripts/install-rerank-sidecar.sh &

# Code knowledge graph
bash scripts/install-graphify.sh

# T4 seed
.venv/bin/aiforge memory reindex-code --repo aiforge --root .

# Auto-start on reboot
bash scripts/install-sidecar-agents.sh install

# Register v4.1 agents in Paperclip
bash scripts/paperclip-bootstrap-v41.sh

# Auto-refresh on commit
bash scripts/install-post-commit-hook.sh
```

---

## Repo layout

| Path | Purpose |
|------|---------|
| `aiforge_core/store_v2.py` | 4-tier memory store + proposals |
| `aiforge_core/retrieval.py` | BM25 + vec + RRF + rerank per role |
| `aiforge_core/context.py` | prompt assembler + compaction |
| `aiforge_core/reflection.py` | FactExtract XML parser + proposal writer |
| `aiforge_core/embed.py` | single `embed()` helper → :8764 |
| `aiforge_core/rag.py` | AST-aware codebase indexer → T4 |
| `aiforge_core/lifecycle.py` | parent + child state machines |
| `aiforge_core/retry.py` | loop caps, breaker, kill switch, confidence |
| `aiforge_core/config.py` | paperclip.config.yml loader |
| `aiforge_core/cli.py` | `aiforge memory …` subcommand |
| `agents/architect/` | system prompts (cloud + local_30b), permissions |
| `agents/sr-developer/` | decomposition prompt, permissions |
| `agents/developer/` | implementation prompt, permissions |
| `agents/fact-extract/` | reflection XML prompt, permissions |
| `mcp/memory-server.json` | search_memory + append_event |
| `mcp/rag-server.json` | search_code |
| `mcp/graphify-server.json` | search_graph |
| `mcp/git-tools.json` | git_diff + run_tests + run_command |
| `services/embed_sidecar/` | bge-m3 ONNX FastAPI |
| `services/rerank_sidecar/` | bge-reranker FastAPI |
| `scripts/launchd/` | LaunchAgent plists |
| `scripts/install-*.sh` | bootstrap + sidecar + graphify installers |
| `scripts/paperclip-bootstrap-v41.sh` | register + configure 4 v4.1 agents |
| `scripts/flip-architect-mode.sh` | swap Architect cloud ↔ local_30b |
| `scripts/install-post-commit-hook.sh` | auto graphify + T4 reindex |
| `scripts/install-sidecar-agents.sh` | install + start LaunchAgents |
| `docs/runbook.md` | full v4.1 operational guide |
| `docs/superpowers/specs/` | v4.1 design spec |
| `docs/superpowers/plans/` | phased impl plan |
| `tests/python/test_integration.py` | wire-to-wire integration suite |

Runtime state (gitignored): `.aiforge/`, `graphify-out/cache/`, `.venv/`, `~/.paperclip/`.

---

## Test harness

```bash
.venv/bin/pytest tests/python/ -v -m "not live_sidecar"
# 62 tests (62 pass)

# With live infra on Mac Studio
pytest tests/python/test_embed_sidecar.py tests/python/test_rerank_sidecar.py -v -m live_sidecar
# Contract tests against running sidecars
```

Integration test (`tests/python/test_integration.py`) exercises every layer wire-to-wire with mocks: embed → store → retrieve_for_role → assemble_prompt → reflection → propose.

---

## Key shortcuts

- **Paperclip UI:** http://localhost:3100 (direct) or http://paperclip.local (Caddy)
- **Mac Studio SSH:** `manikanta@192.168.70.185` (override `SSH_HOST=...`)
- **Architect mode:** `AIFORGE_ARCHITECT_MODE={cloud,local_30b}` (orchestrator) or `scripts/flip-architect-mode.sh` (Paperclip)
- **Global kill:** `touch ~/AIForgeCrew/.aiforge/KILL` on Mac Studio
- **Per-ticket kill:** `kill` label in Paperclip

## License

MIT — see [`LICENSE`](./LICENSE).
