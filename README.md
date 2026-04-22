# AIForgeCrew

**Autonomous AI dev team.** Human files a ticket → 5 AI agents triage, plan, implement, review, and learn. Supervisor → Planner → Doer → Feedback → Learner, each a different model family. Single parent-ticket thread, sub-tickets per work unit, cross-session memory, code knowledge graph, hybrid retrieval. Runs on one Mac Studio (M3 Ultra, 96 GB). Laptop = remote control.

**Stack:** Custom Python orchestrator + FastAPI + React/Vite UI.
**Runbook:** [`docs/runbook.md`](./docs/runbook.md)

---

## TL;DR — first ticket in 10 minutes

```bash
# Mac Studio (once)
cd ~/AIForgeCrew && git pull
bash scripts/runtime/install.sh         # Postgres schema + models + migrations + tick timers
bash scripts/runtime/install-ui.sh      # Node + Vite build + API LaunchAgent

# Open the UI (via SSH tunnel from laptop)
ssh -fNL 8799:127.0.0.1:8799 manikanta@<mac-studio-ip>
open http://127.0.0.1:8799/ui/

# Or file a ticket from the CLI (assignee optional — defaults to supervisor)
python -m aiforge_core.runtime.cli create \
  --title "…" --body "…" --priority medium
```

Tick timers run every 60 s. Watch events land in the UI's Logs view or via `tail -f ~/.aiforge/logs/orchestrator-*.ndjson | jq`.

---

## How it works

```
Ticket in UI / CLI
   │
   ▼
aiforge.tickets  (Postgres, ONE-<n> ids)
   │
   ▼  launchd timer (60s/role)
python -m aiforge_core.runtime <role>
   │
   ├── claim_next() → oldest todo for the role
   ├── build context bundle   (aiforge-deep-context → T4 + graphify + claude-memory)
   ├── LLM tool loop          (openai-client → LM Studio, or claude --print subprocess)
   │      tools: search, read_file, edit, write_file, run_shell, fetch_url,
   │             git_commit, create_child_ticket, post_comment, set_status, retain_fact
   │      loop-guard + deadline watchdog + wall-clock timeout
   ├── structured JSON log    (~/.aiforge/logs/orchestrator-<role>.ndjson)
   └── exits; next tick picks up next todo
```

---

## Agents

Five-role pipeline (Supervisor → Planner → Doer → Feedback → Learner). Cross-family models. All local by default; Supervisor can flip to cloud Claude via env.

| Role | Model | Family | Ctx | Transport | Max turns | Purpose |
|---|---|---|---|---|---|---|
| Supervisor | gemma-3-12b-it | Google dense (12B) | 16K | OpenAI-compat → LM Studio | 15 | Triage + rescue stuck tickets + audit completed tickets |
| Planner | openai/gpt-oss-20b | OpenAI open dense (20B) | 32K | OpenAI-compat → LM Studio | 25 | Deep analysis + child-ticket decomposition; can escalate to Supervisor when stuck |
| Doer | qwen3-coder-next | Alibaba dense | 128K | OpenAI-compat → LM Studio | 60 | Implementation, compile-gated commit; can escalate to Planner on spec gap |
| Feedback | openai/gpt-oss-20b (shared with Planner) | OpenAI open dense (20B) | 16K | OpenAI-compat → LM Studio | 6 | Audits Doer's diff + tests; pass or fail back; reliable tool-call protocol |
| Learner | phi-4-mini-reasoning | Microsoft dense (3.8B) | 16K | OpenAI-compat → LM Studio | 4 | Post-merge fact distillation → T3 memory; dedup search before retain |

Prompts live in `aiforge_core/runtime/roles.py`. Tool allowlists in `config.py` per role.

### Flow
```
human files ticket
    ↓  (assignee_role defaults to supervisor)
Supervisor  — triage (Case A) OR rescue stuck ticket (Case B, label=supervisor-help) OR audit (Case C)
    ↓
Planner     — analysis comment + 1-2 child tickets (each with project + ## Scope/Files/Acceptance)
    ↓        if stuck → escalate back to Supervisor with "supervisor-help" label
Doer        — memory-first → grep → read → edit → COMPILE (mandatory) → commit (never broken) → comment
    ↓        if compile still red after 2 retries → escalate back to Planner with "doer-blocked" label
Feedback    — git show --stat + read_file + compile verify → verdict_pass or verdict_fail (scope-creep = auto-fail)
    ↓  pass: status=in_review + auto-queue Learner sibling
    ↓  fail: back to Doer with fixlist in metadata.feedback_fixlist
Learner     — search-for-dup → retain_fact × 1-3 + post_comment + set_status(done)
```

**Escalation loops (v2)**: Planner can bounce a ticket to Supervisor when it can't identify target repo / file anchors. Doer can bounce to Planner on compile-red or spec gap. Supervisor fills the missing pieces (project, file anchors, revised Scope) and routes the ticket back. Prevents agents from silently blocking on ambiguity.

3-month trajectory: swap Supervisor to local planner model — already local by default.

---

## Memory — one Postgres, one `memories` table, pgvector HNSW

| Tier | Wing pattern | Source | Lifetime |
|---|---|---|---|
| **T1 episodic** | `ticket/<id>` | orchestrator tool-call events | ticket lifetime |
| **T2 canon** | `rules/canon`, `rules/*` | curated + Planner / Supervisor `retain_fact` | permanent |
| **T3 skills/recipes** | `skills/*`, `patterns/*` | Sr Dev / Dev / Fact Extract `retain_fact` | permanent |
| **T4 code chunks** | `code/<repo>`, `code/claude-memory` | `scripts/bulk-index-all-repos.sh` | rebuilt on post-commit hook |
| **Graph** | per-repo `graphify-out/graph.json` | graphify CLI | rebuilt on post-commit hook |

Retrieval: bge-m3 (dim 1024, sidecar `:8764`) for embeddings + BM25 + RRF + bge-reranker-v2-m3 (sidecar `:8765`) FP16. Per-role tier policy in `aiforge_core/retrieval.py:ROLE_POLICIES`.

---

## Ticket workflow

```
┌─ todo ─┐
│        ▼
│     in_progress  ◄── agent picks up
│        │
│        ▼
│     in_review  ◄── agent hands off (comment posted, code committed)
│        │
│        ▼
│       done     ◄── human or Fact Extract closes
│
└──► blocked / cancelled  (manual intervention)
```

Status transitions are **agent-driven via the `set_status` tool**. The orchestrator does NOT auto-move tickets — that prevents false blocks under model latency.

Branch convention: `aiforge/<PARENT_ID>-<slug>`. All children of the same parent share it. Doer commits locally; a human pushes + PRs.

---

## Services

| Port | What | Owner |
|---|---|---|
| 1234 | LM Studio (local inference) | `lms server` |
| 5432 | Postgres + pgvector (aiforge DB) | homebrew postgres |
| 8764 | bge-m3 embed sidecar | launchd `com.aiforge.embed-sidecar` |
| 8765 | bge-reranker-v2-m3 sidecar | launchd `com.aiforge.rerank-sidecar` |
| 8799 | FastAPI + React UI | launchd `com.aiforge.api` |
| — | 5× per-role tick timers | launchd `com.aiforge.tick-<role>` (60-120 s interval) |
| — | Daily reindex @ 02:00 | launchd `com.aiforge.reindex-daily` |

---

## RAM guard (memguard)

`aiforge_core/runtime/memguard.py` runs at every tick-start (before LLM). Parses `lms ps`, applies a 1.15× overhead factor on loaded weights, evicts LRU non-protected models if loading the target would exceed `AIFORGE_RAM_BUDGET_GB` (default 75). On LM Studio load failure, retries-with-evict up to 3 attempts. Hard `AIFORGE_RAM_CEILING_GB=85` on (active + wired) also triggers eviction.

Protected: `qwen3-coder-next` only (Doer, 45 GB, 8h TTL). **Planner + Feedback share `openai/gpt-oss-20b`** (one 12 GB slot, JIT-loaded, plan_rebalance keeps it warm while queue has work). Supervisor (`gemma-3-12b-it`, 8 GB) and Learner (`phi-4-mini-reasoning`, 2 GB) JIT-load on demand. 2× Doer + 2× Planner workers via `AIFORGE_TICK_INSTANCE=a|b` plists; `--parallel 4` on every `lms load` so one model slot serves concurrent inference.

---

## Context management

**Per-tick assembly** (`orchestrator._build_context_bundle`):
1. `aiforge-deep-context` CLI — role-tuned RAG over all memory tiers, reranked.
2. Linked tickets (parent + siblings + children + embedding-related via T1 wing).
3. Graph hint (graphify call-site JSON pointer if repo has one).
4. Last 20 events tail.
5. FEEDBACK FIXLIST section on Doer retry (if prior feedback_fail).
6. Pre-built DIGEST embedded in Learner ticket body at verdict_pass time.

**Per-role ctx window** (`config.RoleConfig.ctx`): supervisor/feedback/learner 16K, planner 32K, doer 128K. LM Studio loads each at role-sized window via `lms load --context-length`.

**Tiny-model trim**: supervisor/feedback/learner skip the heavy deep-context CLI block to stay within 16K.

**Mid-tick compaction** (`orchestrator._compact_old_tool_results`): after turn 15, tool result messages older than the last 5 get truncated to 400 chars + `[elided N chars — re-call tool if needed]`. Saves 50-80% prompt tokens on long ticks without dropping the tool-call chain.

**Per-role output caps** (`llm.ROLE_MAX_TOKENS`): supervisor 400, planner 1500, doer 2000, feedback 600, learner 800. Prevents reasoning models from going verbose (learner phi-4-mini was generating 5000+ tokens per turn = 37s; capped at 800 = ~8s).

---

## Memory + auto-learn

**Four tiers** (single Postgres `memories` table, pgvector HNSW):
- `t1` episodic (wing=`ticket/<id>`) — auto-written by orchestrator at finalize
- `t2` canon (wing=`rules/*`) — seeded + Supervisor/Planner retain
- `t3` skills/patterns (wing=`skills/<service>`, `patterns/<topic>`) — Learner + Planner retain
- `t4` code chunks (wing=`code/<repo>`) — indexed by bulk/incremental reindex

**Retrieval** (`retrieval.retrieve_for_role`): role-tuned BM25 + vector per tier → RRF fuse → bge-reranker-v2-m3 top-k. Gracefully degrades: unknown role → planner policy, sidecar down → RRF fallback, per-tier errors skipped.

**Fact hit-tracking**: every successful `search()` bulk-UPDATEs hit_count + last_hit_at on returned memory rows. Query via `/api/metrics → top_facts_by_hits`.

**Dead-fact archival**: daily reindex cron runs `archive_dead_facts(age_days=90)`. Retained t2/t3 memories with zero hits after 90 days → wing=`archived/<original>`. Still searchable but deprioritized.

**Auto-queue Learner**: on `verdict_pass`, orchestrator creates a Learner ticket with a pre-built DIGEST (parent + siblings + commits + files) so Learner has everything to emit retain_facts without spelunking.

**Supervisor decision trace**: every `update_assignee` writes a memory to wing=`decisions/supervisor`. Future supervisor ticks retrieve via search for consistent routing.

---

## Metrics + ops

- `GET /api/metrics` — ticket grid × role/status, feedback verdict ratios, stop_reason distribution, reclaim histogram, memory hit-rate per tier, top 10 facts by hits, 24h activity per role.
- `GET /api/agents` — live model matrix (reads `config.ROLES`).
- `GET /api/health` — postgres + LM Studio HTTP status.
- Structured ndjson logs per role under `~/.aiforge/logs/orchestrator-*.ndjson`. `jq -c` friendly.
- Every `llm.turn` carries `msg_chars`, `msg_count`, `tokens_in`, `tokens_out`, `dur_ms` for ctx + latency monitoring.

---

## Watchdogs

- `com.aiforge.pg-watchdog` — polls `pg_isready` every 30s; clears stale `postmaster.pid` + `brew services restart postgresql@16` on failure.
- `com.aiforge.lmstudio` — polls `/v1/models` every 30s; restarts `lms server start` on failure.
- Both `KeepAlive=true` so launchd respawns the watchdog itself if it crashes.

History notes:
- Planner was initially `qwen3.6-35b-a3b` (vision-capable → LM Studio rejects `numParallelSessions > 1`). Swapped to `openai/gpt-oss-20b` (non-vision dense 20B) so planner-a + planner-b can run concurrent inference.
- Feedback was initially `gemma-4-e4b-it-mlx` (4B edge). It kept ending ticks silent model_done instead of calling `verdict_pass/verdict_fail` — too small for reliable structured-output protocol. Swapped to `openai/gpt-oss-20b` (shared with Planner, no extra RAM), plus added orchestrator fallback: if feedback model_done with comment but no verdict tool, implicit_pass + queue Learner.

Env: `AIFORGE_RAM_BUDGET_GB`, `AIFORGE_MEMGUARD_DISABLE=1` (bypass).

Emits structured log events: `memguard.load`, `memguard.unload`, `memguard.evict`, `memguard.over_budget`.

---

## UI

React 18 + Vite + TypeScript, served by FastAPI at `/ui/`.

- **Dashboard** — Postgres + LM Studio health, agent cards, recent tickets, memory wings
- **Tickets** — list w/ role+status filter, inline "New ticket" form
- **Ticket detail** — body, children, full event timeline, comment box, one-click status transitions
- **Agents** — 5 role cards (model, tool allowlist, open tickets, live-log link)
- **Logs** — live SSE tail per role, structured event render
- **Memory** — bge-reranked semantic search across all tiers

Zero UI library (~150 lines of CSS). Dark theme.

---

## CLI (no UI needed)

```bash
python -m aiforge_core.runtime.cli create --title "…" --assignee sr_developer
python -m aiforge_core.runtime.cli list --role sr_developer --status todo,in_progress
python -m aiforge_core.runtime.cli show ONE-123
python -m aiforge_core.runtime.cli comment ONE-123 --body "…"
python -m aiforge_core.runtime.cli status ONE-123 --status done
python -m aiforge_core.runtime      sr_developer      # manual one-shot tick
```

---

## Dev & ops

```bash
# Structured logs
tail -f ~/.aiforge/logs/orchestrator-*.ndjson \
  | jq -c '{ts, role, ticket, event, tool, turn, dur_ms, tokens_out}'

# Per-ticket replay
psql aiforge -c "
  SELECT created_at, agent_role, kind, left(body, 200)
    FROM ticket_events
   WHERE ticket_id = (SELECT id FROM tickets WHERE identifier='ONE-123')
   ORDER BY created_at"

# Manual tick
python -m aiforge_core.runtime sr_developer

# Kill switch (all agents)
for r in architect sr_developer developer fact_extract; do
  launchctl bootout gui/$(id -u)/com.aiforge.tick-$r
done
launchctl bootout gui/$(id -u)/com.aiforge.api
```

Model hot-swap: edit `aiforge_core/runtime/config.py:ROLES` → `git push` → pull on Mac Studio → `launchctl kickstart -k gui/$(id -u)/com.aiforge.tick-<role>`.

---

## Access from laptop

The API binds `0.0.0.0:8799`, but macOS firewall on the Mac Studio silently drops unsolicited inbound LAN packets. Simplest: SSH tunnel.

```bash
# laptop
ssh -fNL 8799:127.0.0.1:8799 manikanta@<mac-studio-ip>
open http://127.0.0.1:8799/ui/
```

(Optional) nicer local hostname: `echo '127.0.0.1 aiforge.local' | sudo tee -a /etc/hosts` → http://aiforge.local:8799/ui/.

---

## Repository layout

```
aiforge_core/
├── runtime/
│   ├── orchestrator.py       # single-tick runner (lock, claim, worktree, tool-loop)
│   ├── llm.py                # LM Studio client + claude subprocess adapter
│   ├── tools.py              # @register tool catalogue (11 tools)
│   ├── roles.py              # per-role system prompts + build_messages()
│   ├── tickets.py            # Postgres CRUD
│   ├── memory.py             # search + retain_fact
│   ├── config.py             # role matrix, DSNs, budgets, tool allowlists
│   ├── logging_setup.py      # JSON-per-line logger
│   ├── api.py                # FastAPI: health, tickets, memory, SSE logs, /ui/
│   ├── cli.py                # `python -m aiforge_core.runtime.cli`
│   └── __main__.py           # `python -m aiforge_core.runtime <role>`
├── embed.py retrieval.py store_v2.py …   (reused from earlier tier-indexer)

db/migrations/2026-04-21-tickets.sql       # tickets + ticket_events + counter

scripts/runtime/
├── install.sh                # first-time Mac Studio provisioning
├── install-launchd.sh        # LaunchAgent installer
├── install-ui.sh             # Node + Vite build + API LaunchAgent
├── com.aiforge.tick-<role>.plist   # 5 per-role timers
├── com.aiforge.api.plist     # FastAPI LaunchAgent
├── com.aiforge.reindex-daily.plist # daily memory reindex @ 02:00
├── reindex-daily.py          # reindex claude-memory + project-rules + aiforge
└── embed-backfill.py         # bge-m3 backfill for NULL embeddings

web/                          # Vite + React UI
├── src/views/{Dashboard,Tickets,TicketDetail,Agents,Logs,Memory}.tsx
├── src/{main.tsx,api.ts,styles.css}
└── vite.config.ts package.json tsconfig.json

docs/
└── runbook.md                # full operational guide
```

---

## Failure-mode quick table

| Symptom | Where to look | Fix |
|---|---|---|
| Tick idle | `~/.aiforge/logs/launchd-tick-<role>.log` + `cli list --role <role>` | no `todo` for that role |
| LLM 400 ctx overflow | `llm.error` event on ticket | raise LM Studio ctx or tighten prompt |
| UI blank | browser console | hard-reload (Cmd+Shift+R) — old cached bundle |
| UI can't reach API | laptop curl health | SSH tunnel (see Access from laptop) |
| Doer hits max_turns | `tick.end.stop_reason=max_turns` | tighten role prompt or raise `TICK_MAX_TURNS` |
| Two ticks collide | `lock.skip` event | expected — per-role fcntl lock |

---

## License

See `LICENSE`.
