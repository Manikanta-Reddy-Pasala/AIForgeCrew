# Architecture — crib sheet

Terse module map. Full rationale in [`DESIGN.md`](../DESIGN.md).

## Data plane

```
user
  │ paperclip CLI
  ▼
paperclip.store   ─────►  .paperclip/paperclip.db  (tickets · comments · audit)
  │                             ▲
  │ lifecycle.advance()          │ audit_event()
  ▼                             │
paperclip.lifecycle  ─ calls ─► retry.enforce_loop_caps + require_coverage_for_mr
  │                             ▲
  │ advance → assignee changes   │
  ▼                             │
paperclip.store.assign()  ─────┘
```

## Agent turn (Hermes)

```
Paperclip ticket ─► Agent.run(ticket_id, msg)
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
                       │                                     (file ACL + blocked paths)
                       │                                           │
                       │                                           ▼ audit_event('tool_call')
                       │                                     result → back into messages
                       │
                       └─ budget.assert_within_budget() + record()
                       (loop ≤ 15 tool calls / 3 rounds)
```

## Module one-liners

| Module | One-line |
|---|---|
| `paperclip.store` | SQLite WAL, single-writer txns, tickets/comments/audit |
| `paperclip.config` | Loads `paperclip.config.yml` into typed dataclasses |
| `paperclip.lifecycle` | DESIGN §4 state machine; transitions enforce loop caps + coverage gate |
| `paperclip.permissions` | `role_can()` + `file_access()`; blocked paths win over allow lists |
| `paperclip.budget` | Per-ticket tokens + per-month USD; raises BudgetExceeded |
| `paperclip.retry` | enforce_loop_caps, require_coverage_for_mr, CircuitBreaker |
| `paperclip.mem` | Two-tier MemPalace wrapper with writer ACL |
| `paperclip.rag` | ChromaDB PersistentClient; reindex + query |
| `paperclip.crg` | AST call graph; blast_radius + dependency_chain |
| `paperclip.git_ops` | git subprocess ops; per-path ACL before `git add` |
| `paperclip.safety` | scrub_ticket_text + assert_no_network_tools |
| `paperclip.observe` | ticket_report + fleet_summary from audit rows |
| `paperclip.cli` | `paperclip` entrypoint |
| `hermes.llm` | OpenAI-compat client (LM Studio + cloud), stdlib urllib |
| `hermes.tools` | ToolRegistry + handlers (read/write file, run tests, rag, crg, git) |
| `hermes.agent` | Per-role driver; owns the tool-call loop |
| `hermes.cli` | `hermes` entrypoint |

## State directories (gitignored)

| Path | Owner | Rebuild with |
|---|---|---|
| `.paperclip/paperclip.db` | Paperclip | `rm -rf .paperclip/ && paperclip doctor` |
| `.aiforge/mem/{project,agent/<role>}/` | MemPalace | `make mempalace-install` |
| `.aiforge/rag/` | RAG / ChromaDB | `make rag-reindex` |
| `.aiforge/crg/graph.json` | code-review-graph | auto-rebuilt on first call |
| `.venv/` | uv | `make paperclip-install` |

## Config files (source of truth)

| File | Consumers | Invariant |
|---|---|---|
| `paperclip.config.yml` | paperclip runtime | Org chart + budgets + retry_rules + routing |
| `agents/<role>/permissions.yml` | permissions.py | Matches DESIGN §5.2 matrix exactly |
| `security/file-access-rules.yml` | permissions.file_access | Per-role read/write globs |
| `security/blocked-paths.yml` | permissions.file_access | `globally_blocked` wins over role allows |
| `security/model-checksums.yml` | download/verify/load scripts | path, source_url, sha256, role |
| `memory/mem0-config.yml` | MemBus | Writers of project memory, palace paths |

## Enforcement points

| Rule | Where |
|---|---|
| Role ↔ permission matrix | `tools/check_permission_matrix.py` (CI) |
| File ACL on every tool call | `hermes/tools.py` → `paperclip.permissions.file_access` |
| No network tools in registry | `tools/audit_tool_network.py` (CI) |
| EM cloud scrub | `hermes/agent.py::Agent.run` (if role == em) |
| Loop caps | `paperclip.lifecycle.advance` → `retry.enforce_loop_caps` |
| Coverage ≥80 before MR | `paperclip.lifecycle.advance` → `retry.require_coverage_for_mr` |
| Token/USD budget | `hermes/agent.py::Agent.run` → `budget.assert_within_budget` |
| Circuit breaker | `paperclip.retry.CircuitBreaker` (audit-persisted) |
| sha256 match before startup | `scripts/verify-checksums.sh` |
