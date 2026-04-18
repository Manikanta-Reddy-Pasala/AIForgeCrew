# Architecture — crib sheet

Terse module map. Full rationale in [`DESIGN.md`](../DESIGN.md).

Three layers cooperate:

- **Paperclip** (external, Node + React UI, Mac Studio :3100) — org chart,
  tickets, dashboard, audit, budgets, `hermes_local` adapter.
- **Hermes Agent** (external, Node CLI, Mac Studio) — actual agent runtime.
  30+ native tools, 80+ skills, MCP client/server, session persistence via
  `--resume`. Paperclip's `hermes_local` adapter shells into it.
- **aiforge_core** (our Python) — policy layer loaded into Hermes as a
  skill pack at `~/.hermes/skills/aiforge/`. Enforces DESIGN §§4, 5, 8, 10.

## Data plane

```
Paperclip (:3100, Postgres)
    ▲
    │  canonical ticket state + audit
    │
    ▼  hermes_local adapter spawns Hermes CLI (--resume <session>)
┌──────────────────────────────────────────────────────────────────┐
│  Hermes Agent (Node CLI)                                         │
│    native tools + MCP servers +                                  │
│    ~/.hermes/skills/aiforge/  (calls into aiforge_core)          │
└──────────────────────────────────────────────────────────────────┘
    │ aiforge_core.store mirror ────►  .paperclip/paperclip.db
    │                                  (tickets / comments / audit)
    │                                         ▲
    │ lifecycle.advance()                      │ audit_event()
    ▼                                          │
aiforge_core.lifecycle  ─► retry.enforce_loop_caps
                           retry.require_coverage_for_mr
```

## Agent turn (via Hermes)

```
Paperclip task assigned to <role>
                │
                ▼
         Hermes CLI `-q` single-query + `--resume <sid>`
                │
                ├─ Hermes loads native tools + ~/.hermes/skills/aiforge/*
                │    (our skills call aiforge_core.{mem, rag, crg, git_ops, net, safety, budget, retry})
                │
                ├─ aiforge_core.safety.scrub_ticket_text()  (if role == em, before cloud call)
                ├─ aiforge_core.mem.wake_up(role) + search(q)   (injected at session boot)
                │
                ▼
         LLM round-trip (LM Studio :1234 for local roles, cloud for EM)
                │
                ├─ tool_calls? → dispatch → aiforge_core.permissions.file_access gate
                │                           budget.assert_within_budget
                │                           audit_event('tool_call')
                │
                ▼
         Hermes exits → adapter posts result to Paperclip → state transition
                        (lifecycle.advance validates loop caps + coverage gate first)
```

## Module one-liners (aiforge_core)

| Module | One-line |
|---|---|
| `aiforge_core.store` | SQLite WAL mirror, single-writer txns, tickets/comments/audit |
| `aiforge_core.config` | Loads `paperclip.config.yml` into typed dataclasses |
| `aiforge_core.lifecycle` | DESIGN §4 state machine; transitions enforce loop caps + coverage gate |
| `aiforge_core.permissions` | `role_can()` + `file_access()`; blocked paths win over allow lists |
| `aiforge_core.budget` | Per-ticket tokens + per-month USD; raises BudgetExceeded |
| `aiforge_core.retry` | enforce_loop_caps, require_coverage_for_mr, CircuitBreaker |
| `aiforge_core.mem` | Two-tier MemPalace wrapper with writer ACL |
| `aiforge_core.rag` | ChromaDB PersistentClient; reindex + query |
| `aiforge_core.crg` | AST call graph; blast_radius + dependency_chain |
| `aiforge_core.git_ops` | git subprocess ops; per-path ACL before `git add` |
| `aiforge_core.net` | Allowlisted GET/HEAD fetch (network_fetch capability) |
| `aiforge_core.safety` | scrub_ticket_text |
| `aiforge_core.observe` | ticket_report + fleet_summary from audit rows |
| `aiforge_core.cli` | `aiforge` entrypoint |

## State directories (gitignored)

| Path | Owner | Rebuild with |
|---|---|---|
| `.paperclip/paperclip.db` | aiforge_core | `rm -rf .paperclip/ && aiforge doctor` |
| `.aiforge/mem/{project,agent/<role>}/` | MemPalace | `make mempalace-install` |
| `.aiforge/rag/` | RAG / ChromaDB | `make rag-reindex` |
| `.aiforge/crg/graph.json` | code-review-graph | auto-rebuilt on first call |
| `.venv/` | uv | `make aiforge-install` |
| `~/.paperclip/` (Mac Studio) | real Paperclip | `make paperclip-install` |
| `~/.hermes/` (Mac Studio) | real Hermes Agent | `make hermes-install` |

## Config files (source of truth)

| File | Consumers | Invariant |
|---|---|---|
| `paperclip.config.yml` | aiforge_core runtime | Org chart + budgets + retry_rules + routing |
| `agents/<role>/permissions.yml` | permissions.py | Matches DESIGN §5.2 matrix exactly |
| `security/file-access-rules.yml` | permissions.file_access | Per-role read/write globs |
| `security/blocked-paths.yml` | permissions.file_access | `globally_blocked` wins over role allows |
| `security/network-allowlist.yml` | aiforge_core.net | Domains allowed for fetch_url |
| `security/model-checksums.yml` | download/verify/load scripts | path, source_url, sha256, role |
| `memory/mem0-config.yml` | MemBus | Writers of project memory, palace paths |

## Enforcement points

| Rule | Where |
|---|---|
| Role ↔ permission matrix | `tools/check_permission_matrix.py` (CI) |
| File ACL on every tool call | `aiforge_core.permissions.file_access` (called from Hermes skill) |
| EM cloud scrub | `aiforge_core.safety.scrub_ticket_text` (called before cloud LLM) |
| Loop caps | `aiforge_core.lifecycle.advance` → `retry.enforce_loop_caps` |
| Coverage ≥80 before MR | `aiforge_core.lifecycle.advance` → `retry.require_coverage_for_mr` |
| Token/USD budget | `aiforge_core.budget.assert_within_budget` (called from Hermes skill) |
| Circuit breaker | `aiforge_core.retry.CircuitBreaker` (audit-persisted) |
| sha256 match before startup | `scripts/verify-checksums.sh` |
| Outbound HTTP (GET/HEAD, allowlist) | `aiforge_core.net.fetch_url` + `security/network-allowlist.yml` |
