# AIForgeCrew

Autonomous AI dev team. Human files ticket → AI agents plan, write tests (TDD),
implement, review, open MR. All threaded under one ticket.

Full architecture: [`DESIGN.md`](./DESIGN.md). Ops guide: [`docs/runbook.md`](./docs/runbook.md).

## Flow

```
human ticket → EM plans → Tester writes failing tests → Sr Dev makes them pass
   → Tester verifies (≥80% cov) → Sr Architect reviews → MR → human merges
```

One ticket. One audit trail. No sub-tickets.

## Agents + models

| Role | Model (MLX, local) | Size | Why |
|---|---|---:|---|
| Engineering Manager | Cloud (Claude/GPT) | — | Plans, never sees code |
| Tester | GLM-4.7-Flash (MoE 3B active) | 24 GB | Best open tool-use (Playwright MCP) |
| Sr Developer | Qwen3.6-35B-A3B (MoE 3B active) | 20 GB | Top open SWE-bench 73.4% |
| Sr Architect | Gemma-4-31B dense | 18 GB | Dense → deep review reasoning |
| Embed | nomic-embed-text v1.5 | 0.08 GB | RAG |

All roles except EM run on the Mac Studio (LM Studio OpenAI-compat :1234).

## Components

| Component | What it does | Code |
|---|---|---|
| **Paperclip** | Ticket store + lifecycle SM + audit + budgets | `paperclip/` |
| **Hermes** | Per-agent driver: loads prompts, tool-call loop, LLM calls | `hermes/` |
| **MemPalace** | Two-tier memory (project shared + per-role). ACL: EM+Arch write project | `paperclip/mem.py` + `.aiforge/mem/` |
| **RAG** | ChromaDB semantic search over docs + agent configs | `paperclip/rag.py` + `.aiforge/rag/` |
| **code-review-graph** | Python AST call graph → blast radius / dependency chain | `paperclip/crg.py` |
| **Git MCP** | Role-scoped git ops (branch, commit, create_mr) | `paperclip/git_ops.py` |
| **Safety** | Prompt-injection scrub + network-tool audit | `paperclip/safety.py` |
| **Retry** | Loop caps, circuit breaker, coverage gate | `paperclip/retry.py` |
| **Observability** | Per-ticket + fleet reports from audit table | `paperclip/observe.py` |

## How each part works — crisp

### Paperclip
SQLite at `.paperclip/paperclip.db`. 3 tables: `tickets`, `comments`, `audit`
(append-only). Every state transition + tool call + budget spend records an
audit row. State machine in `lifecycle.py` enforces DESIGN §4 — invalid
transitions raise before DB commit.

### Hermes
`Agent.load(repo, role)` reads `agents/<role>/system-prompt.md` + `contract.md`,
builds the tool registry, calls LM Studio with `tools=[...]`. When model
returns `tool_calls`, Hermes dispatches through `paperclip.permissions` and
appends each result as a `role=tool` message. Loops until `tool_calls=[]` or
checkpoint cap (15 tool calls). Budget enforced every round.

### MemPalace
5 palaces under `.aiforge/mem/`: `project/` + `agent/{role}/`. `MemBus.remember(role, scope, text)`
fails early if writer ACL rejects (only EM + Sr Architect write project).
`search(role, q, scope='auto')` hits own + project palaces. Hermes injects
`wake_up() + search(user_message)` into system prompt before every turn.

### RAG
`RagIndex(repo).reindex()` chunks markdown/yml at 1200 chars with 200 overlap,
stores in ChromaDB PersistentClient. `query(q, top_k=5)` returns `Chunk(source, text)`.
Embedder = ChromaDB bundled (fully local). Registered as `rag_query` Hermes tool.

### code-review-graph
`build_graph(repo)` walks every `.py` (excluding `.venv/.aiforge/node_modules`),
uses stdlib `ast` to collect `FunctionDef + Call` nodes. `blast_radius(g, target, max_depth=3)`
finds upstream callers — tells you what breaks if you change target. Registered
as `blast_radius` + `dependency_chain` Hermes tools.

### Git MCP
`GitOps(repo).commit(role, paths, msg)` validates every path against role's
file-access glob before `git add -- <paths>`. Tester may only commit `tests/**`,
Sr Dev only `src/**`, Architect only opens MRs via `gh pr create`. No `git add .`
anywhere. Network-free until create_mr.

### Safety
`scrub_ticket_text()` redacts "ignore all instructions", "reveal system prompt",
jailbreak + exfil patterns, strips NUL + C0 chars, caps 32K chars. EM agent runs
this before every cloud call. `assert_no_network_tools(registry)` introspects
handler source — fails fast if any imports urllib/requests/httpx/socket.

### Retry + coverage gate
Before every transition: `enforce_loop_caps` counts verifying↔coding and
reviewing↔coding loops from audit; >3 → RetryExceeded + auto-escalate.
Before mr_created: `require_coverage_for_mr` demands latest `coverage` audit
event ≥80%. `CircuitBreaker` tracks per-(role, ticket) consecutive failures;
trips at 3, only human `reset()` clears it.

### Observability
`ticket_report(store, TID)` aggregates: tokens per role, tool-call counts,
transitions, loop counters, duration, comment count. `fleet_summary(store, cfg)`
rolls up all tickets + flags stalled ones (>60 min inactivity per retry_rules).
CLI emits JSON for `jq` or dashboard panels.

## How to use it

Everything is script-driven — no manual config edits.

### 1. First-time bring-up (fresh Mac Studio)

```bash
# On the Mac Studio
curl -LsSf https://astral.sh/uv/install.sh | sh       # uv, no Xcode
caffeinate -dimsu &                                   # prevent sleep
# Install LM Studio from lmstudio.ai (one-time GUI install)

# Clone the repo
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew
cd AIForgeCrew

# Everything else
make paperclip-install       # Paperclip + Hermes CLIs
make mempalace-install       # MemPalace + 5 palaces
make rag-install             # ChromaDB + initial index
make models                  # ~65 GB MLX models + verify + load + health
```

Can also drive all of that over SSH from any dev machine — override the target:

```bash
make models SSH_HOST=user@your-mac-studio.local
```

### 2. Daily ops

```bash
# Health checks
make validate permission-check audit-tools          # config + permissions + network audit
make paperclip-doctor                               # DB + config sanity
make health                                         # per-role LLM probe

# Ticket CLI
paperclip ticket create --title "Add JWT auth" --body "login endpoint + middleware"
paperclip ticket list --state reviewing
paperclip ticket show TICKET-xxx
paperclip ticket advance TICKET-xxx --to planning --actor em
paperclip ticket comment TICKET-xxx --author em --body "Plan ready"
paperclip audit TICKET-xxx
paperclip report-ticket TICKET-xxx | jq .
paperclip report-fleet     | jq .
paperclip budget-report --role sr_developer

# Run a single agent turn
hermes run --role sr-developer --ticket TICKET-xxx --message "Make failing tests pass"
hermes tools --role tester      # show tools visible to the role

# Rebuild RAG after editing docs
make rag-reindex
```

### 3. Benchmarks

```bash
make bench                   # solo per role (TTFT + tok/s)
make bench-concurrent        # paired throughput (DEV+TESTER, DEV+ARCH, TESTER+ARCH)
make bench-passk             # pass@1 over docs/eval/tickets/
MODEL=zai-org/glm-4.7-flash make bench-passk
```

### 4. Full regression

```bash
make validate permission-check audit-tools
.venv/bin/pytest tests/python/ -v       # 72 tests
```

### 5. When things break

See [`docs/runbook.md`](./docs/runbook.md) §2 failure playbook:
SSH timeout, LM Studio model issues, budget blew up, circuit breaker tripped,
coverage gate blocks MR, stale ticket.

## Command reference

`make help` — full target list. Groups:

```
Dev:               setup, lint, test, validate, permission-check, audit-tools
P0 models:         models, download, verify, server, load, health, bench*
P1 paperclip:      paperclip-install, paperclip-test, paperclip-doctor, paperclip
P2 hermes:         hermes-test, hermes
P3 mempalace:      mempalace-install, mempalace-test
P4 rag + crg:      rag-install, rag-reindex, rag-query, crg-query
```

## Repo layout

| Path | Purpose |
|------|---------|
| `paperclip/` | Orchestrator runtime (tickets, lifecycle, mem, rag, crg, git, safety, retry, observe, CLI) |
| `hermes/` | Agent runtime (LLM client, tool registry, agent driver, CLI) |
| `agents/<role>/` | system-prompt.md, contract.md, permissions.yml |
| `security/` | File access rules, blocked paths, model checksums |
| `memory/` | MemPalace config + agent schemas |
| `mcp/` | MCP server manifests (tool contracts) |
| `observability/` | Dashboard + alerts config |
| `scripts/` | All install + benchmark + health scripts (macOS-only) |
| `tools/` | Schema validators, permission matrix, tool-network audit |
| `tests/` | pytest + bats — 72 python tests |
| `docs/` | Runbook, model-evaluation, hardware-guide, security-policy, troubleshooting, eval/tickets |

Runtime state (gitignored): `.paperclip/` · `.aiforge/` · `.venv/`

## License

MIT — see [`LICENSE`](./LICENSE).
