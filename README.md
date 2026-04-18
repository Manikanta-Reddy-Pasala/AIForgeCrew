# AIForgeCrew

Autonomous AI dev team. Human files ticket → AI agents plan, write tests (TDD),
implement, review, open MR. All threaded under one ticket.

Full architecture: [`DESIGN.md`](./DESIGN.md). Ops guide: [`docs/runbook.md`](./docs/runbook.md).

## Flow — how it works

```
  Human
    │
    │  create ticket in Paperclip UI @ http://localhost:3100
    ▼
┌────────────────────────────────────────────────────────────────────┐
│  PAPERCLIP (Node + React UI, embedded Postgres, trusted-loopback)   │
│  Company = OneShell. One ticket, audit trail, org chart, budgets.   │
└────────────────────────────────────────────────────────────────────┘
    │  hermes_local adapter dispatches task →
    ▼
┌────────────────────────────────────────────────────────────────────┐
│  aiforge_core + hermes (Python, Hermes-side runtime)                │
│  Agent tool-call loop against LM Studio :1234 (Mac Studio M3 Ultra) │
└────────────────────────────────────────────────────────────────────┘
    │
    │  assignee = Engineering Manager
    ▼
  [EM]           plans subtasks + acceptance criteria + test scenarios
    │           (cloud LLM; ticket text scrubbed first)
    │           comments on ticket, advances → tests_writing
    ▼
  [Tester]       writes failing unit + integration tests
    │           (Hermes → GLM-4.7-Flash; git commits to tests/**)
    │           comments "N tests, all failing", advances → coding
    ▼
  [Sr Developer] reads failing tests, writes prod code
    │           (Hermes → Qwen3.6-35B-A3B; git commits to src/**)
    │           comments "code ready", advances → verifying
    ▼
  [Tester]       re-runs pytest, records coverage event (≥80 required)
    │           pass → advances → reviewing
    │           fail → loops back to coding (max 3× per §10)
    ▼
  [Sr Architect] reviews code + tests + blast radius
    │           (Hermes → Gemma-4-31B; read-only)
    │           approve → advances → mr_created, gh pr create
    │           reject → loops back to coding (max 3×)
    ▼
  Human          merges MR
```

One ticket. One audit trail. No sub-tickets. Paperclip stores the canonical
ticket state in embedded Postgres; `aiforge_core` mirrors tool calls + budget
spend into `.paperclip/paperclip.db` for local §10 gates + observability.

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

| Component | What it does | Where |
|---|---|---|
| **Paperclip** (external) | Org chart UI, ticket store, goals, agent adapters, dashboard | Node.js app on Mac Studio :3100 |
| **aiforge_core** | §4 lifecycle SM + audit + budgets + retry + coverage gate + observe | `aiforge_core/` |
| **Hermes** | Per-agent driver: prompts, tool-call loop, LLM calls | `hermes/` |
| **MemPalace** | Two-tier memory (project shared + per-role) via MemPalace 3.3 + 29 MCP tools | `aiforge_core/mem.py` + `.aiforge/mem/` |
| **RAG** | ChromaDB semantic search over docs + agent configs | `aiforge_core/rag.py` + `.aiforge/rag/` |
| **code-review-graph** | Python AST call graph → blast radius / dependency chain | `aiforge_core/crg.py` |
| **Git ops** | Role-scoped git (branch, commit, create_mr) via subprocess | `aiforge_core/git_ops.py` |
| **Safety** | Prompt-injection scrub + network-tool audit | `aiforge_core/safety.py` |
| **Retry** | Loop caps, circuit breaker, coverage gate | `aiforge_core/retry.py` |
| **Observability** | Per-ticket + fleet reports | `aiforge_core/observe.py` |
| **Bridge** | Poll Paperclip tasks → Hermes run → report back | `aiforge_core/bridge.py` |

## How each part works — crisp

### Paperclip (external, Node + React UI)
Real Paperclip ([github.com/paperclipai/paperclip](https://github.com/paperclipai/paperclip),
MIT) running on the Mac Studio. Trusted-loopback mode → :3100, embedded
Postgres → :54329. Holds the canonical org chart (OneShell + 4 agents),
tickets, goals, budgets, dashboard, audit. REST API at `/api/companies`,
`/api/agents`, etc. `hermes_local` adapter ships with Paperclip and is how
it hands tasks to our Python runtime.

### aiforge_core
Our Hermes-side runtime. SQLite mirror at `.paperclip/paperclip.db` captures
tool calls + budget spend + coverage events so §10 gates (loop caps,
coverage ≥80, circuit breaker) run locally before Paperclip accepts a
state transition. State machine in `aiforge_core/lifecycle.py` enforces
DESIGN §4 — invalid transitions raise before the DB commit.

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
# On the Mac Studio (one-time)
curl -LsSf https://astral.sh/uv/install.sh | sh       # uv (no Xcode CLT needed)
caffeinate -dimsu &                                   # prevent sleep
# Install LM Studio from lmstudio.ai                   (GUI install → ships `lms` CLI)

# From any dev machine (or on the Mac Studio itself)
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew
cd AIForgeCrew

make aiforge-install          # .venv + aiforge + hermes CLIs
make mempalace-install        # MemPalace + 5 palaces (1 shared + 4 per-role)
make rag-install              # ChromaDB + first RAG reindex
make models                   # ~65 GB MLX models + sha256 verify + server + load + health

make paperclip-install        # real Paperclip UI (Node 20 via fnm + npx paperclipai)
make paperclip-start          # server on Mac Studio :3100
make paperclip-bootstrap      # create OneShell company + 4 agents (idempotent)
make paperclip-tunnel         # ssh -L 3100 so laptop browser can reach the UI
```

All `SSH_HOST` defaults to `manikanta@192.168.70.185`; override per invocation:

```bash
make models SSH_HOST=user@your-mac-studio.local
```

### 2. Daily ops

```bash
# Health checks
make validate permission-check audit-tools          # config + permissions + network audit
make aiforge-doctor                                 # DB + config sanity
make paperclip-status                               # real Paperclip health
make health                                         # per-role LLM probe

# Tickets — via Paperclip UI (http://localhost:3100 through the tunnel) or REST API:
curl -X POST http://localhost:3100/api/companies/$COMPANY_ID/issues \
     -H 'Content-Type: application/json' \
     -d '{"title":"Add JWT auth","body":"login endpoint + middleware"}'

# Local aiforge CLI (mirrors Paperclip state + exposes §10 gates + reports)
aiforge ticket list --state reviewing
aiforge ticket show TICKET-xxx
aiforge ticket advance TICKET-xxx --to planning --actor em
aiforge audit TICKET-xxx
aiforge report-ticket TICKET-xxx | jq .
aiforge report-fleet  | jq .
aiforge budget-report --role sr_developer

# Run a single agent turn (bypasses Paperclip — useful for debugging)
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
Dev:              setup, lint, test, validate, permission-check, audit-tools
P0 models:        models, download, verify, server, load, health, bench*, bench-passk
aiforge-core:     aiforge-install, aiforge-test, aiforge-doctor, aiforge -- ARGS
hermes:           hermes-test, hermes -- ARGS
memory:           mempalace-install, mempalace-test
rag + crg:        rag-install, rag-reindex, rag-query, crg-query
Paperclip UI:     paperclip-install, paperclip-start, paperclip-stop, paperclip-status
                  paperclip-bootstrap (create OneShell + 4 agents)
                  paperclip-tunnel    (ssh -L 3100 → laptop browser)
```

## Repo layout

| Path | Purpose |
|------|---------|
| `aiforge_core/` | Hermes-side runtime: lifecycle, store, mem, rag, crg, git, safety, retry, observe, bridge, CLI |
| `hermes/` | Agent runtime: LLM client, tool registry, agent driver, CLI |
| `agents/<role>/` | system-prompt.md, contract.md, permissions.yml |
| `security/` | File access rules, blocked paths, model checksums |
| `memory/` | MemPalace config + agent schemas |
| `mcp/` | MCP server manifests (tool contracts) |
| `observability/` | Dashboard + alerts config |
| `scripts/` | Install + benchmark + Paperclip bootstrap scripts (macOS-only) |
| `tools/` | Schema validators, permission matrix, tool-network audit |
| `tests/` | pytest + bats — 72 python tests |
| `docs/` | Runbook, architecture, model-evaluation, hardware-guide, security-policy, troubleshooting, eval/tickets |

Runtime state (gitignored): `.paperclip/` · `.aiforge/` · `.venv/` · `~/.paperclip/` (real Paperclip, Postgres)

## License

MIT — see [`LICENSE`](./LICENSE).
