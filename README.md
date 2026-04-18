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

| Role | Primary (local MLX) | Size | Fallback (cloud) |
|---|---|---:|---|
| Engineering Manager | Claude Opus 4.7 via Claude Code subscription | — | — |
| Tester | GLM-4.7-Flash (MoE 3B active) | 24 GB | NVIDIA NIM `minimax-m2.7` (230B) |
| Sr Developer | Qwen3.6-35B-A3B (MoE 3B active) | 20 GB | NVIDIA NIM `minimax-m2.7` (230B) |
| Sr Architect | Gemma-4-31B dense | 18 GB | NVIDIA NIM `minimax-m2.7` (230B) |
| Embed | nomic-embed-text v1.5 | 0.08 GB | — |

All primary (non-EM) inference hits LM Studio :1234 on the Mac Studio. EM
uses Claude Code OAuth (no separate API key) by default; override with
`EM_PROVIDER=anthropic` or `nvidia` at configure time.

**Fallback** (cloud NVIDIA NIM, 230B MoE) auto-selected by
`aiforge_core.retry.pick_profile()` after 2 failed dev↔tester or dev↔architect
loops — one retry below DESIGN §10's hard escalate-to-human cap.

## Components

| Component | What it does | Where |
|---|---|---|
| **Paperclip** (external) | Org chart UI, ticket store, goals, agent adapters, dashboard | Node app on Mac Studio :3100 |
| **Hermes Agent** (external) | 30+ tools, 80+ skills, session resume, MCP client/server | Node CLI on Mac Studio |
| **hermes-paperclip-adapter** (external) | Wires Paperclip's `hermes_local` agent type to the Hermes CLI | npm global |
| **aiforge_core** | §4 lifecycle + audit + budgets + retry + coverage gate + fallback routing + observability | `aiforge_core/` |
| **aiforge skills** (for Hermes) | 8 Markdown skills wrapping aiforge_core — lifecycle, coverage, rag, crg, memory, git, fetch, report | `aiforge_core/skills/` → `~/.hermes/skills/aiforge/` |
| **MemPalace** | Two-tier memory (project shared + per-role) via MemPalace 3.3 | `aiforge_core/mem.py` + `.aiforge/mem/` |
| **RAG** | ChromaDB semantic search over docs + agent configs | `aiforge_core/rag.py` + `.aiforge/rag/` |
| **code-review-graph** | Python AST call graph → blast radius / dependency chain | `aiforge_core/crg.py` |
| **Git ops** | Role-scoped git (branch, commit, create_mr) via subprocess | `aiforge_core/git_ops.py` |
| **Safety** | Prompt-injection scrub for EM cloud path | `aiforge_core/safety.py` |
| **Retry** | Loop caps, circuit breaker, coverage gate | `aiforge_core/retry.py` |
| **Net** | Allowlisted outbound HTTP for Tester + Sr Dev | `aiforge_core/net.py` |
| **Fetch** | GET/HEAD against `security/network-allowlist.yml` | exposed as Hermes skill |

## How each part works — crisp

### Paperclip (external, Node + React UI)
[github.com/paperclipai/paperclip](https://github.com/paperclipai/paperclip),
MIT. Runs on Mac Studio :3100, trusted-loopback, embedded Postgres :54329.
Holds canonical org chart (OneShell + 4 agents), tickets, goals, budgets,
dashboard, audit. REST API `/api/companies`, `/api/agents`, etc.

### Hermes Agent (external, Node CLI)
[github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent),
MIT. The real agent runtime: 30+ native tools (file, shell, memory, etc.),
80+ skills (togglable from Paperclip UI), session persistence via
`--resume`, MCP client + server mode (`hermes mcp serve` exposes sessions
to Claude Desktop / Cursor / VS Code). Config at `~/.hermes/`.

### hermes-paperclip-adapter
The glue: Paperclip's `hermes_local` adapter type shells out to the Hermes
CLI with `-q --resume` per heartbeat. Our 4 Paperclip agents already use
`adapterType=hermes_local` — installing this adapter is what makes that
wiring actually dispatch.

### aiforge_core (our policy layer)
Not an agent runtime — it's the DESIGN-specific policy enforcement exposed
to Hermes as a skill set. SQLite mirror at `.paperclip/paperclip.db`
captures tool calls + budget + coverage so §10 gates (loop caps, coverage
≥80, circuit breaker) run before Paperclip accepts a state transition.
Lifecycle state machine in `aiforge_core/lifecycle.py` rejects invalid
transitions before DB commit.

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

make aiforge-install          # .venv + aiforge CLI (policy / reports)
make mempalace-install        # MemPalace + 5 palaces (1 shared + 4 per-role)
make rag-install              # ChromaDB + first RAG reindex
make models                   # ~65 GB MLX models + sha256 verify + server + load + health

make paperclip-install        # real Paperclip UI (Node 20 via fnm + npx paperclipai)
make paperclip-start          # server on Mac Studio :3100
make paperclip-bootstrap      # create OneShell company + 4 agents (idempotent)

make hermes-install           # real Hermes Agent CLI (30+ tools, 77 skills)
make hermes-adapter-install   # wires Paperclip's hermes_local → Hermes CLI
make deploy-mac-studio        # clone repo + install aiforge on the Mac Studio
make hermes-skills-install    # 8 aiforge skills → ~/.hermes/skills/aiforge/

# Provider wiring.
# With NVIDIA key: EM + fallback both use NVIDIA NIM (no OAuth needed).
NVIDIA_API_KEY=nvapi-... make hermes-configure

# Alt: EM via OpenAI Codex CLI (ChatGPT Plus subscription, OAuth):
EM_PROVIDER=openai-codex make hermes-configure
make hermes-login             # runs `hermes login --provider openai-codex` with TTY

# Alt: EM via direct Anthropic API (separate key, not Claude Code subscription —
#      Hermes login only supports nous + openai-codex OAuth):
ANTHROPIC_API_KEY=sk-ant-... make hermes-configure

# Hermes web dashboard (agent config, keys, sessions, insights) — runs on Mac Studio
make hermes-dashboard-start
make hermes-dashboard-tunnel  # ssh -L 9119 so laptop browser can reach it

# Paperclip UI (org chart, tickets, runs, audit)
make paperclip-tunnel         # ssh -L 3100 so laptop browser can reach it
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

# Run a single Hermes turn directly (bypasses Paperclip — useful for debugging)
hermes -q "Make the failing tests in tests/test_foo.py pass"
hermes --resume            # continue last session

# Expose Hermes sessions to Claude Desktop / Cursor / VS Code via MCP
hermes mcp serve           # stdio MCP server

# Hermes's own web dashboard (local on Mac Studio :9119)
hermes dashboard --port 9119

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
Dev:              setup, lint, test, validate, permission-check
P0 models:        models, download, verify, server, load, health, bench*, bench-passk
aiforge-core:     aiforge-install, aiforge-test, aiforge-doctor, aiforge -- ARGS
memory:           mempalace-install, mempalace-test
rag + crg:        rag-install, rag-reindex, rag-query, crg-query
Paperclip UI:     paperclip-install, paperclip-start, paperclip-stop, paperclip-status
                  paperclip-bootstrap (create OneShell + 4 agents)
                  paperclip-tunnel    (ssh -L 3100 → laptop browser)
Real Hermes:      hermes-install, hermes-adapter-install,
                  deploy-mac-studio, hermes-skills-install, hermes-configure,
                  hermes-login (OAuth for EM=openai-codex),
                  hermes-dashboard-start / -stop / -tunnel (web UI :9119)
```

## Repo layout

| Path | Purpose |
|------|---------|
| `aiforge_core/` | Policy layer: lifecycle, store, mem, rag, crg, git, safety, retry, observe, net, CLI |
| `agents/<role>/` | system-prompt.md, contract.md, permissions.yml |
| `security/` | File access rules, blocked paths, network allowlist, model checksums |
| `memory/` | MemPalace config + agent schemas |
| `mcp/` | MCP server manifests (tool contracts) |
| `observability/` | Dashboard + alerts config |
| `scripts/` | Install + benchmark + Paperclip/Hermes bootstrap scripts (macOS-only) |
| `tools/` | Schema validators, permission matrix |
| `tests/` | pytest + bats — 68 python tests |
| `docs/` | Runbook, architecture, model-evaluation, hardware-guide, security-policy, troubleshooting, eval/tickets |

Runtime state (gitignored): `.paperclip/` · `.aiforge/` · `.venv/`
External-tool state on Mac Studio: `~/.paperclip/` (Postgres) · `~/.hermes/` (Hermes Agent sessions/skills)

## License

MIT — see [`LICENSE`](./LICENSE).
