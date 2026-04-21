# AIForgeCrew

**Autonomous AI dev team.** Human files a ticket → 5 AI agents triage, plan, implement, review, and learn. Supervisor → Planner → Doer → Feedback → Learner, each a different model family. Single parent-ticket thread, sub-tickets per work unit, cross-session memory, code knowledge graph, hybrid retrieval. Runs on one Mac Studio (M3 Ultra, 96 GB). Laptop = remote control.

**Stack:** v5 (2026-04-21) — custom Python orchestrator + FastAPI + React/Vite UI. No external agent wrapper.
**Runbook:** [`docs/runbook-v5.md`](./docs/runbook-v5.md)

---

## TL;DR — first ticket in 10 minutes

```bash
# Mac Studio (once)
cd ~/AIForgeCrew && git pull
bash scripts/runtime/install-v5.sh      # Postgres schema + models + migrations + tick timers
bash scripts/runtime/install-ui.sh      # Node + Vite build + API LaunchAgent

# Open the UI (via SSH tunnel from laptop)
ssh -fNL 8799:127.0.0.1:8799 manikanta@<mac-studio-ip>
open http://127.0.0.1:8799/ui/

# Or file a ticket from the CLI
python -m aiforge_core.runtime.cli create \
  --title "…" --body "…" --assignee sr_developer --priority medium
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
| Supervisor | gemma-4-26b-a4b-it | Google MoE (~4B active) | 32K | OpenAI-compat → LM Studio | 4 | Triage + route + standards enforcement |
| Planner | qwen3.6-35b-a3b | Alibaba MoE (~3B active) | 64K | OpenAI-compat → LM Studio | 25 | Deep analysis + child-ticket decomposition |
| Doer | qwen3-coder-next | Alibaba dense | 128K | OpenAI-compat → LM Studio | 40 | Implementation + tests + commit |
| Feedback | gemma-4-26b-a4b-it | Google MoE (~4B active) | 16K | OpenAI-compat → LM Studio | 6 | Audits Doer's diff + tests; pass or fail back |
| Learner | phi-4-mini-reasoning | Microsoft dense (3.8B) | 16K | OpenAI-compat → LM Studio | 4 | Post-merge fact distillation → T3 memory |

Prompts live in `aiforge_core/runtime/roles.py`. Tool allowlists in `config.py` per role.

### Flow
```
human files ticket
    ↓  (assignee_role defaults to supervisor)
Supervisor  — triage + update_assignee + 1-sentence brief
    ↓
Planner     — analysis comment + N child tickets (assignee=doer)
    ↓
Doer        — edit + compile + test + commit + post_comment
    ↓  (orchestrator auto-routes to feedback)
Feedback    — read_file + run test verify → verdict_pass or verdict_fail
    ↓  pass: status=in_review + auto-queue Learner sibling
    ↓  fail: back to Doer with fixlist in metadata.feedback_fixlist
Learner     — retain_fact × N + post_comment + set_status(done)
```

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

`aiforge_core/runtime/memguard.py` runs at every tick-start (before LLM). Parses `lms ps`, applies a 1.4× overhead factor on loaded weights, evicts LRU non-protected models if loading the target would exceed `AIFORGE_RAM_BUDGET_GB` (default 70). On LM Studio load failure, retries-with-evict up to 3 attempts. Protected: `qwen3-coder-next`, `qwen3.6-35b-a3b` (Doer + Planner always hot). Supervisor + Feedback share one `gemma-4-26b-a4b-it` slot. Learner (`phi-4-mini-reasoning`) JIT-loads on demand.

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

agents/<role>/system-prompt.md             # source prompts (copied into roles.py)

db/migrations/2026-04-21-tickets.sql       # tickets + ticket_events + counter

scripts/runtime/
├── install-v5.sh             # first-time Mac Studio provisioning
├── install-v5-launchd.sh     # LaunchAgent installer
├── install-ui.sh             # Node + Vite build + API LaunchAgent
├── cleanup-v4.sh             # retire legacy services (backup-first)
├── com.aiforge.tick-<role>.plist   # 4 per-role timers
├── com.aiforge.api.plist     # FastAPI LaunchAgent
├── migrate-*-to-aiforge.*     # one-shot v4 → v5 data imports
└── embed-backfill.py          # bge-m3 backfill for NULL embeddings

web/                          # Vite + React UI
├── src/views/{Dashboard,Tickets,TicketDetail,Agents,Logs,Memory}.tsx
├── src/{main.tsx,api.ts,styles.css}
└── vite.config.ts package.json tsconfig.json

docs/
└── runbook-v5.md             # full operational guide
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
