# Memory System — AIForgeCrew Pipeline v4

> **Superseded by v4.1.** Current design lives in [`docs/superpowers/specs/2026-04-21-autonomous-memory-orchestration-design.md`](../superpowers/specs/2026-04-21-autonomous-memory-orchestration-design.md) §3.
>
> **Summary of v4.1 changes:**
> - Retired Hindsight + Chroma. Single store: Postgres 17 + pgvector + pg_trgm.
> - 4 tiers: **T1 episodic** (per-ticket, 7-day TTL) · **T2 semantic** (human-gated distilled facts) · **T3 procedural** (recipes) · **T4 codebase** (AST-chunked).
> - Hybrid retrieval: BM25 + vector → RRF → bge-reranker-v2-m3 cross-encoder.
> - Per-role retrieval policies in `aiforge_core/retrieval.py::ROLE_POLICIES`.
> - Write gating: agents append T1 directly; T2/T3 only via reflection proposals with human approval.
> - Graphify code KG added as **understanding layer** (search_graph MCP tool) — orthogonal to T4 execution retrieval.
> - Single embedding model: bge-m3 (1024-d ONNX), replaces LM Studio nomic.

The rest of this document is the historical v4 design kept for archaeology.

---

Design (2026-04-20) after v3 failure: shared `aiforge` hindsight bank leaked agent identity across roles ("Tester agent..." burned into Developer's memory). Every agent read stale facts from prior role runs. Root cause: ONE bank, NO isolation.

## Goals

1. **Per-role isolation** — Sr Dev's memory can't pollute Developer (and vice versa).
2. **Ticket-scoped context** — every agent starts each ticket with the right inputs injected, no guessing.
3. **Cross-session persistence** — lessons from merged PRs survive for future tickets.
4. **Deterministic retrieval** — same query returns same hits; no hidden state.
5. **Cheap enough** — no extra infra beyond what Mac Studio + LM Studio already runs.

## 6 memory layers

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1: SYSTEM PROMPT (per-role AGENTS.md, static)           │
│  Layer 2: TICKET CONTEXT (docs/tickets/<ID>.md, Architect)     │
│  Layer 3: BREAKDOWN (docs/breakdowns/<ID>.md, Sr Dev output)   │
│  Layer 4: CODEBASE RAG (aiforge-rag ChromaDB, ~3k chunks)      │
│  Layer 5: FACT MEMORY (Hindsight, per-agent bank)              │
│  Layer 6: GIT HISTORY (commits, PR descriptions)               │
└─────────────────────────────────────────────────────────────────┘
```

### Layer 1: System Prompt (static)

- File: `~/.paperclip/.../agents/<id>/instructions/AGENTS.md`
- Content: role charter (what this agent does), hard rules, non-goals, codebase map (CODEBASE_INDEX.md appended).
- Scope: per-agent, per-role. Edited by human only.
- Isolation: ✓ — each agent has its own file.

### Layer 2: Ticket Context (per-ticket, Architect writes)

- File: `docs/tickets/<TICKET-ID>.md` in AIForgeCrew repo.
- Template: `docs/tickets/TEMPLATE.md`.
- Content: problem statement, design choice, acceptance criteria, involved repos, file paths, reference patterns, constraints/non-goals, test strategy hint.
- Scope: per-ticket. Only the Architect (Claude Code, cloud) writes it.
- Injection: every agent's FIRST mandated tool call is `cat docs/tickets/<TICKET-ID>.md`.
- Isolation: ✓ — ticket-specific file.

### Layer 3: Breakdown (per-ticket, Sr Dev writes)

- File: `docs/breakdowns/<TICKET-ID>.md` committed to branch `aiforge/<TICKET-ID>`.
- Content: numbered sub-tasks (≤15 min each), per-sub-task target file:line + expected change + test case in plain English.
- Scope: per-ticket. Only Sr Dev writes. Developer reads.
- Injection: Developer's second mandated tool call is `cat docs/breakdowns/<TICKET-ID>.md`.
- Isolation: ✓ — ticket-specific file.

### Layer 4: Codebase RAG

- Backend: ChromaDB at `~/AIForgeCrew/.aiforge/rag/` (local embedded, no network).
- Index scope: AIForgeCrew docs + PosPythonBackend + TallyConnector + MongoDbService + PosDataSyncService.
- Chunking: Java method-boundary for `.java`; char-sliding 2500/300 for others. Multi-repo with `<repo>:<path>` prefix.
- Access: `rag "<query>"` CLI (`~/.local/bin/rag`). Returns top-k hits with cited source path.
- Refresh: human-triggered via `python scripts/rag-reindex-multi.py`. Not auto.
- Isolation: shared across agents (read-only). No contamination possible.

### Layer 5: Fact Memory (Hindsight — per-agent bank)

**Current state**: single shared bank `aiforge`. CAUSES THE LEAK.

**New design**: per-agent bank.

Config at `~/.hermes/hindsight/config.json`:
```json
{
  "mode": "local_embedded",
  "bank_id": "aiforge-<role>",   // NEW: varies by agent
  ...
}
```

Per-agent profiles at `~/.hermes/profiles/<role>.memory.yaml`:
```yaml
memory:
  provider: hindsight
  bank: aiforge-srdev        # or aiforge-developer, aiforge-architect
  session_namespace: srdev
```

Storage: pgvector in pg0 Postgres. Banks map to separate `bank_id` columns → no cross-bank reads.

Access: `hindsight_recall`, `hindsight_retain` tools in Hermes — already wired.

**What goes in each bank**:
- `aiforge-srdev`: "when I see ticket about X, break it down like Y", tool preferences, common pitfalls.
- `aiforge-developer`: "when fixing Java reactive bug, use pattern Y", commit-message conventions learned.
- `aiforge-architect`: (not applicable — Claude Code doesn't use hindsight bank; relies on this session + MEMORY.md files under ~/.claude/).

**What does NOT go in hindsight** (use other layers):
- Agent identity (comes from AGENTS.md system prompt)
- Ticket content (comes from docs/tickets/*.md)
- Codebase state (comes from RAG or git)

### Layer 6: Git History

- `git log` / `git blame` / `gh pr view` — always authoritative.
- Pre-merge: Developer's commits on `aiforge/<TICKET-ID>`.
- Post-merge: `git log --oneline -- <path>` shows who changed what.
- Scope: shared across all agents + humans. Read-only for agents.
- No explicit injection — agents use `git log` via `terminal` tool when needed.

## Retrieval protocol (what every agent does first)

```
1. cat docs/tickets/<TICKET-ID>.md          # Layer 2
2. cat docs/breakdowns/<TICKET-ID>.md       # Layer 3 (Developer only; Sr Dev skips since it writes this)
3. hindsight_recall("<topic>") × 1-3        # Layer 5 (per-agent bank)
4. rag "<query>" × 1-3                      # Layer 4
5. Read ≤3 anchor files named in Layer 2    # Direct file reads
```

Each layer is narrow + cheap. No layer can LEAK into another.

## Per-ticket lifecycle

```
Architect  Sr Dev     Developer   Human
   │         │           │          │
   │ write   │           │          │
   ├─→ L2 ───┤           │          │
   │         │ read L2   │          │
   │         │ rag (L4)  │          │
   │         │ hindsight (L5 srdev) │
   │         │ write L3  │          │
   │         ├─→ L3 ─────┤          │
   │         │           │ read L2+L3│
   │         │           │ rag (L4) │
   │         │           │ hindsight (L5 dev)
   │         │           │ code + test + PR
   │         │           ├─→ git ──┤
   │         │           │         │ review + merge
   │         │           │         │ POST: hindsight_retain in L5 (lessons learned)
```

## Post-merge learning hook (not yet built)

After PR merged:
- Trigger: `gh pr merge` or webhook
- Action: append "lesson learned" to the responsible agent's hindsight bank via `hindsight_retain`:
  - `aiforge-srdev`: "Tickets about X often involve files Y. Check Z first."
  - `aiforge-developer`: "Pattern A works for bug B."

Mechanism (future work):
- Shell hook post-merge: `curl POST /api/hindsight-retain` with summary extracted from PR body.
- OR human reviewer hand-authors the lesson in a comment; extractor script harvests.

## What is NEW vs v3

| Aspect | v3 | v4 |
|--------|----|----|
| Hindsight bank | single `aiforge` | per-agent `aiforge-srdev`, `aiforge-developer` |
| Ticket context | none (agents guess) | `docs/tickets/<ID>.md` (Architect writes) |
| Breakdown | inline in ticket comment | committed file `docs/breakdowns/<ID>.md` |
| Architect | EM paused, Thinker=local gemma | Claude Code external, no local agent |
| Role count | 3 agents + EM | 2 local agents + external Architect |

## Implementation status

| Layer | Status |
|-------|--------|
| 1 System prompt | ✓ written, 2 files deployed |
| 2 Ticket context | ✓ template + ONE-52 example written |
| 3 Breakdown | ✓ directory created, Sr Dev writes on dispatch |
| 4 Codebase RAG | ✓ functional, `rag` CLI working |
| 5 Fact memory (per-agent banks) | ⚠️ NOT YET — shared bank still active. Next step. |
| 6 Git history | ✓ native git |

## Open work

- Per-agent hindsight bank config: modify `~/.hermes/hindsight/config.json` at dispatch time (different bank per agent role), OR use per-profile memory.yaml files to override. Simplest: let `srdev-run.sh` set `HINDSIGHT_BANK=aiforge-srdev` env var before launching hermes; `dev-run.sh` sets `aiforge-developer`.
- Post-merge learning extractor (not critical for now).
- Validate end-to-end on ONE-52.
