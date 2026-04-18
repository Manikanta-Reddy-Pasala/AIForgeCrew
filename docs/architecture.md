# Architecture — crib sheet

Terse module map. Full rationale in [`DESIGN.md`](../DESIGN.md).

Two runtimes cooperate:

- **Paperclip** (external, Node.js + React UI on Mac Studio :3100) — org chart,
  tickets, dashboard, audit, budget tracking, `hermes_local` adapter.
- **aiforge_core + hermes** (our Python, Hermes-side) — agent tool-call loop,
  file ACL enforcement, memory, RAG, CRG, git ops, §10 gates.

## Data plane

```
Paperclip (:3100, Postgres)
     ▲
     │  ticket state, comments, audit (canonical)
     │
     ▼  hermes_local adapter → dispatches to our runtime
┌──────────────────────────────────────────────────────────┐
│  aiforge_core                                            │
│                                                          │
│  aiforge_core.store   ─────►  .paperclip/paperclip.db    │
│       │                (local mirror: tickets / comments │
│       │                / audit — used by §10 gates)      │
│       │                                                  │
│       │ lifecycle.advance()                              │
│       ▼                                                  │
│  aiforge_core.lifecycle  ─► retry.enforce_loop_caps      │
│       │                      retry.require_coverage_for_mr
│       ▼                                                  │
│  aiforge_core.store.assign()                             │
└──────────────────────────────────────────────────────────┘
```

## Agent turn (Hermes)

```
aiforge_core.bridge picks up assigned Paperclip task for <role>
                       │
                       ▼
                   Agent.run(ticket_id, msg)
                       │
                       ├─ mem.wake_up(role) + mem.search(q)  → injected into system prompt
                       ├─ safety.scrub_ticket_text()         → if role == em
                       │
                       ▼
                   LLMClient.chat(model, messages, tools)
                       │
                       ├─ content + tool_calls?              ► tool_calls → dispatch each
                       │                                           │
                       │                                           ▼
                       │                                     ToolRegistry.dispatch()
                       │                                     (capability + file ACL + blocked paths)
                       │                                           │
                       │                                           ▼ audit_event('tool_call')
                       │                                     result → back into messages
                       │
                       └─ budget.assert_within_budget() + record()
                       (loop ≤ 15 tool calls / 3 rounds per §9.3)
                       │
                       ▼
                   bridge.complete(task_id, result) → posts back to Paperclip
```

## Module one-liners

| Module | One-line |
|---|---|
| `aiforge_core.store` | SQLite WAL, single-writer txns, tickets/comments/audit mirror |
| `aiforge_core.config` | Loads `paperclip.config.yml` into typed dataclasses |
| `aiforge_core.lifecycle` | DESIGN §4 state machine; transitions enforce loop caps + coverage gate |
| `aiforge_core.permissions` | `role_can()` + `file_access()`; blocked paths win over allow lists |
| `aiforge_core.budget` | Per-ticket tokens + per-month USD; raises BudgetExceeded |
| `aiforge_core.retry` | enforce_loop_caps, require_coverage_for_mr, CircuitBreaker |
| `aiforge_core.mem` | Two-tier MemPalace wrapper with writer ACL |
| `aiforge_core.rag` | ChromaDB PersistentClient; reindex + query |
| `aiforge_core.crg` | AST call graph; blast_radius + dependency_chain |
| `aiforge_core.git_ops` | git subprocess ops; per-path ACL before `git add` |
| `aiforge_core.safety` | scrub_ticket_text + assert_no_network_tools |
| `aiforge_core.observe` | ticket_report + fleet_summary from audit rows |
| `aiforge_core.bridge` | Polls Paperclip task queue per role → Hermes.Agent.run → reports back |
| `aiforge_core.cli` | `aiforge` entrypoint |
| `hermes.llm` | OpenAI-compat client (LM Studio + cloud), stdlib urllib |
| `hermes.tools` | ToolRegistry + handlers (read/write file, run tests, rag, crg, git) |
| `hermes.agent` | Per-role driver; owns the tool-call loop |
| `hermes.cli` | `hermes` entrypoint |

## State directories (gitignored)

| Path | Owner | Rebuild with |
|---|---|---|
| `.paperclip/paperclip.db` | aiforge_core | `rm -rf .paperclip/ && aiforge doctor` |
| `.aiforge/mem/{project,agent/<role>}/` | MemPalace | `make mempalace-install` |
| `.aiforge/rag/` | RAG / ChromaDB | `make rag-reindex` |
| `.aiforge/crg/graph.json` | code-review-graph | auto-rebuilt on first call |
| `.venv/` | uv | `make aiforge-install` |
| `~/.paperclip/` (Mac Studio) | real Paperclip | `make paperclip-install` |

## Config files (source of truth)

| File | Consumers | Invariant |
|---|---|---|
| `paperclip.config.yml` | aiforge_core runtime | Org chart + budgets + retry_rules + routing |
| `agents/<role>/permissions.yml` | permissions.py | Matches DESIGN §5.2 matrix exactly |
| `security/file-access-rules.yml` | permissions.file_access | Per-role read/write globs |
| `security/blocked-paths.yml` | permissions.file_access | `globally_blocked` wins over role allows |
| `security/model-checksums.yml` | download/verify/load scripts | path, source_url, sha256, role |
| `memory/mem0-config.yml` | MemBus | Writers of project memory, palace paths |

## Enforcement points

| Rule | Where |
|---|---|
| Role ↔ permission matrix | `tools/check_permission_matrix.py` (CI) |
| File ACL on every tool call | `hermes/tools.py` → `aiforge_core.permissions.file_access` |
| No network tools in registry | `tools/audit_tool_network.py` (CI) |
| EM cloud scrub | `hermes/agent.py::Agent.run` (if role == em) |
| Loop caps | `aiforge_core.lifecycle.advance` → `retry.enforce_loop_caps` |
| Coverage ≥80 before MR | `aiforge_core.lifecycle.advance` → `retry.require_coverage_for_mr` |
| Token/USD budget | `hermes/agent.py::Agent.run` → `budget.assert_within_budget` |
| Circuit breaker | `aiforge_core.retry.CircuitBreaker` (audit-persisted) |
| sha256 match before startup | `scripts/verify-checksums.sh` |
