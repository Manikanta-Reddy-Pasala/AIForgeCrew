# AIForge v5 runbook

Hermes-less, Paperclip-less, single-Postgres runtime.

## Stack

| Layer | Component | Port | Owner |
|---|---|---|---|
| Inference | LM Studio | 1234 | local |
| Embeddings | bge-m3 sidecar | 8764 | launchd |
| Rerank | bge-reranker-v2-m3 | 8765 | launchd |
| Storage | aiforge Postgres + pgvector | 5432 | manikanta |
| Orchestrator | `python -m aiforge_core.runtime <role>` | — | launchd timer (60s/role) |
| Cloud fallback | `claude --print` CLI | — | subprocess, subscription auth |

No Paperclip. No hermes. No ChromaDB. No hindsight daemon.

## Agents

| Role | Model | Context | Transport | Max turns |
|---|---|---|---|---|
| Architect | claude-opus-4-7 | — | `claude --print` | 6 |
| Sr Developer | qwen3.6-35b-a3b | 128K | OpenAI-compat → LM Studio | 25 |
| Developer | qwen3-coder-next | 256K | OpenAI-compat → LM Studio | 40 |
| Fact Extract | google/gemma-3-4b-it | 128K | OpenAI-compat → LM Studio | 4 |

## Memory tiers (single Postgres, one `memories` table)

| Tier | Wing | Source |
|---|---|---|
| T1 | `ticket/<id>` | per-ticket facts during a run |
| T2 | `rules/canon`, `rules/*` | migrated hindsight + curated |
| T3 | `skills/*`, `patterns/*` | Fact Extract output + curated |
| T4 | `code/<repo>`, `code/claude-memory` | bulk-index-all-repos |
| graph | `~/codeRepo/<repo>/graphify-out/graph.json` | graphify |

## Install on Mac Studio (first time)

```bash
cd ~/AIForgeCrew && git pull
bash scripts/runtime/install-v5.sh
```

That script:
1. Applies `db/migrations/2026-04-21-tickets.sql`.
2. `pip install openai psycopg[binary] pgvector`.
3. LM Studio unload-all → loads qwen3.6-35b@128K + qwen3-coder-next@256K + gemma-3-4b@128K.
4. Migrates hindsight → aiforge.memories (tier=t2, wing=rules/canon).
5. Runs embed-backfill on null-embedding rows.
6. Installs the 4 launchd timers.

## Retire v4 (after v5 tick is green)

```bash
DRY_RUN=1 bash scripts/runtime/cleanup-v4.sh   # preview
bash scripts/runtime/cleanup-v4.sh              # backup + delete
```

Backups land in `~/.aiforge/backups/YYYY-MM-DD/`:
- `paperclip.sql`, `hindsight.sql` (full DB dumps)
- `hermes.tar.gz`, `aiforge-rag.tar.gz` (filesystem snapshots)

## Daily operation

### File a ticket

```bash
python -m aiforge_core.runtime.cli create \
  --title "Add CDC listener for purchases collection" \
  --body "Extend mongoEventListner to watch purchases … (see existing sales pattern)" \
  --assignee architect --priority medium
```

### Inspect

```bash
python -m aiforge_core.runtime.cli list                     # all recent
python -m aiforge_core.runtime.cli list --role sr_developer --status todo,in_progress
python -m aiforge_core.runtime.cli show ONE-123             # full event log
```

### Comment / move status manually

```bash
python -m aiforge_core.runtime.cli comment ONE-123 --body "looks good, merge"
python -m aiforge_core.runtime.cli status  ONE-123 --status done
```

### Manual single-tick (bypass launchd)

```bash
python -m aiforge_core.runtime sr_developer
```

Useful for one-shot debugging. Honours the same per-role lock — if the launchd
timer is mid-tick, this will no-op.

### Watch the logs

```bash
tail -f ~/.aiforge/logs/orchestrator-*.ndjson \
  | jq -c '{ts, role, ticket, event, tool, turn, dur_ms, tokens_out}'
```

Per-ticket reconstruction:

```sql
SELECT created_at, agent_role, kind, left(body, 200) AS body
FROM ticket_events
WHERE ticket_id = (SELECT id FROM tickets WHERE identifier='ONE-123')
ORDER BY created_at;
```

## Event kinds (for log / SQL filters)

- `tick.start`, `tick.end`, `tick.idle`, `tick.exception`
- `lock.skip`
- `context.built`
- `worktree.prepared`
- `llm.turn`, `llm.error`
- `tool.call`, `tool.result`
- (ticket_events.kind): `comment`, `status_change`, `tool_call`,
  `llm_turn`, `child_created`, `retain`, `error`

## Kill switch

Immediate stop for all agents:

```bash
launchctl bootout gui/$(id -u)/com.aiforge.tick-architect
launchctl bootout gui/$(id -u)/com.aiforge.tick-sr_developer
launchctl bootout gui/$(id -u)/com.aiforge.tick-developer
launchctl bootout gui/$(id -u)/com.aiforge.tick-fact_extract
```

Resume with:

```bash
bash ~/AIForgeCrew/scripts/runtime/install-v5-launchd.sh
```

Per-ticket stop: set status to `cancelled`:

```bash
python -m aiforge_core.runtime.cli status ONE-123 --status cancelled
```

## Model hot-swap

Editing `ROLES` in `aiforge_core/runtime/config.py` is the authoritative way
to change a role's model. Redeploy: `git push → git pull → restart timer`.

Runtime override via adapter metadata is intentionally **not** supported in
v5 — too much foot-gun (cf. v4 `paperclip-bootstrap-v41.sh` regressions).

## Branch convention

One branch per parent ticket: `aiforge/<PARENT_ID>-<slug>`. All child work
shares that branch. Developer does not `git push` automatically — human
reviews the local branch, then pushes + PRs.

## Failure modes and responses

| Symptom | Where to look | Fix |
|---|---|---|
| Tick does nothing | `tail ~/.aiforge/logs/launchd-tick-<role>.log` | usually no todo tickets — `list --role <role>` |
| Tool call fails | `ticket_events` row `kind='tool_call'` + `metadata.error` | fix args in agent prompt or tool impl |
| LLM 400 (context overflow) | `llm.error` event | reduce prompt (context bundle trim) or raise LM Studio context |
| Two ticks collide | `lock.skip` log event | expected — the second tick no-ops |
| Worktree missing | `worktree.prepared` with `path=null` | repo not under `~/codeRepo` — clone it |
