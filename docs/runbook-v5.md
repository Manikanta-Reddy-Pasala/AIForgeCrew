# AIForge v5 runbook

Single-Postgres runtime. Custom Python orchestrator + FastAPI + React/Vite UI. Last update: 2026-04-21.

## Stack

| Layer | Component | Port | Owner |
|---|---|---|---|
| Inference (local) | LM Studio | 1234 | local process |
| Inference (cloud) | `claude --print` CLI subprocess | — | subscription auth |
| Embeddings | bge-m3 sidecar | 8764 | launchd |
| Rerank | bge-reranker-v2-m3 | 8765 | launchd |
| Storage | aiforge Postgres + pgvector | 5432 | homebrew postgres |
| Orchestrator | `python -m aiforge_core.runtime <role>` | — | launchd timer (60 s / role) |
| REST / SSE API | FastAPI (`aiforge_core.runtime.api`) | 8799 | launchd (`com.aiforge.api`) |
| Dashboard UI | React/Vite static (`web/dist/`) | served at `:8799/ui/` | same FastAPI |

v4 cleanup (retires the previous stack — UI daemon on `:3100`, agent wrapper, ChromaDB, hindsight daemon): `scripts/runtime/cleanup-v4.sh`.

## Agents

5-role pipeline, cross-family local models. Supervisor can be flipped to cloud Claude via `AIFORGE_SUPERVISOR_TRANSPORT=claude_cli`.

| Role | Model | Family | Ctx | Transport | Max turns | Role purpose |
|---|---|---|---|---|---|---|
| Supervisor | gemma-4-26b-a4b-it | Google MoE | 32K | OpenAI-compat → LM Studio | 4 | Triage + route + invariant enforcement |
| Planner | qwen3.6-35b-a3b | Alibaba MoE | 64K | OpenAI-compat → LM Studio | 25 | Analysis + child-ticket decomposition |
| Doer | qwen3-coder-next | Alibaba | 128K | OpenAI-compat → LM Studio | 40 | Implementation + tests + commit |
| Feedback | mistralai/devstral-small-2-2512 | Mistral | 32K | OpenAI-compat → LM Studio | 6 | Audit Doer diff + tests; pass / fail-back |
| Learner | qwen/qwen3-4b-thinking-2507 | Alibaba | 16K | OpenAI-compat → LM Studio | 4 | Post-merge fact distillation into T3 |

LM Studio TTL: 8h for hot (Planner, Doer), 30min JIT for tiny (Supervisor, Feedback, Learner). Prompts live in `aiforge_core/runtime/roles.py`.

Legacy role names (`architect`, `sr_developer`, `developer`, `fact_extract`) aliased transparently in `config.py` so pre-rename ticket rows keep working.

### Ticket lifecycle

```
create → assignee=supervisor (default via tickets._apply_supervisor_invariants)
       → supervisor tick: update_assignee(planner|doer|learner) + post_comment + reason
       → planner tick:    analysis comment + N children with assignee=doer
       → doer tick:       edit + test + git_commit + post_comment + set_status(in_review)
                          orchestrator finalize AUTO-ROUTES to assignee=feedback, status=todo
       → feedback tick:   read_file + run_shell(tests) + verdict_pass | verdict_fail
                          pass: status=in_review + auto-queue learner sibling
                          fail: ticket → assignee=doer with feedback_fixlist in metadata
       → learner tick:    retain_fact × 1-5 + post_comment + set_status(done)
```

Supervisor's hard safety rules (enforced in `tickets._apply_supervisor_invariants`, cannot be bypassed by LLM):
- Body containing destructive-intent patterns (`drop table`, `rm -rf /`, `delete all`, credential patterns) → forced `assignee=supervisor` + label `review-required` + `metadata.dangerous_pattern=true`. Never auto-routed.
- Title or body containing `prod|outage|crash|p0|urgent|incident` → auto `priority=urgent` + `metadata.priority_auto_boosted=true`.
- Children inherit parent's assignee (skip re-triage).

## Memory tiers (one Postgres, one `memories` table)

| Tier | Wing pattern | Populator | Lifetime |
|---|---|---|---|
| T1 episodic | `ticket/<id>` | orchestrator tool-calls | ticket lifetime |
| T2 canon | `rules/canon`, `rules/*` | seeded 145 rules + Architect `retain_fact` | permanent |
| T3 skills/patterns | `skills/*`, `patterns/*` | Sr Dev / Developer / Fact Extract `retain_fact` | permanent |
| T4 code | `code/<repo>`, `code/claude-memory` | `scripts/bulk-index-all-repos.sh` | rebuilt on post-commit hook |
| graph | `~/codeRepo/<repo>/graphify-out/graph.json` | graphify CLI | rebuilt on post-commit hook |

Embeddings: bge-m3 (dim 1024). Rerank: bge-reranker-v2-m3 FP16. Retrieval policy per role lives in `aiforge_core/retrieval.py:ROLE_POLICIES`.

## Install on Mac Studio (first time)

```bash
cd ~/AIForgeCrew && git pull
bash scripts/runtime/install-v5.sh      # schema + pip deps + LM Studio loads + migrations + launchd
bash scripts/runtime/install-ui.sh      # brew node + npm install + Vite build + API plist
```

Retire v4 (after v5 tick is green):
```bash
DRY_RUN=1 bash scripts/runtime/cleanup-v4.sh   # preview
bash scripts/runtime/cleanup-v4.sh              # backup + delete
```

Backups land in `~/.aiforge/backups/YYYY-MM-DD/`.

## Legacy `/etc/hosts` cleanup

v4 may have left LAN-hostname aliases you no longer need. On each machine:

```bash
# Drop anything pointing at the old UI/agent hostnames
sudo sed -i.bak '/paperclip\.\(lan\|local\)\|hermes\.\(lan\|local\)/d' /etc/hosts
```

Optional nicer hostname for the new UI (run on the laptop that tunnels in):
```bash
echo '127.0.0.1 aiforge.local' | sudo tee -a /etc/hosts
# then open http://aiforge.local:8799/ui/
```

## Daily operation

### Access the UI

The FastAPI app binds `0.0.0.0:8799` (LAN-reachable) and serves:
- **API:** `http://<mac-studio-ip>:8799/api/health`
- **UI:**  `http://<mac-studio-ip>:8799/ui/`

On the Mac Studio itself: `http://127.0.0.1:8799/ui/`.

### UI views

- **Dashboard** — postgres + LM Studio health, agent cards, recent tickets, memory wings
- **Tickets** — list w/ role+status filter, inline "New ticket" form
- **Ticket detail** — body, children, full event timeline, comment box, one-click status transitions
- **Agents** — 4 role cards (model, tool allowlist, open tickets, live-log link)
- **Logs** — live SSE tail per role, structured event render
- **Memory** — bge-reranked semantic search across tiers

### CLI (no UI needed)

```bash
python -m aiforge_core.runtime.cli create \
  --title "…" --body "…" --assignee sr_developer --priority medium

python -m aiforge_core.runtime.cli list --role sr_developer --status todo,in_progress
python -m aiforge_core.runtime.cli show ONE-123
python -m aiforge_core.runtime.cli comment ONE-123 --body "…"
python -m aiforge_core.runtime.cli status  ONE-123 --status done
python -m aiforge_core.runtime      sr_developer        # manual one-shot tick
```

### Structured logs

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

## Orchestrator internals

Each tick (`python -m aiforge_core.runtime <role>`):

1. fcntl lock at `/tmp/aiforge-tick-<role>.lock` (non-blocking; skip if held).
2. `tickets.claim_next(role)` — oldest `todo` for the role.
3. `tickets.update_status(id, 'in_progress', role=<self>)`.
4. `_ensure_branch_and_worktree(ticket)` — creates `aiforge/<PARENT>-<slug>` branch + worktree under `<repo>/.aiforge-worktrees/<PARENT>`. Children inherit the parent's branch + repo.
5. Context bundle = `aiforge-deep-context "<title>"` (CLI, 150 s timeout).
6. System prompt from `roles.py` + user msg (body + context) + tool JSON schemas (allowlisted).
7. Tool loop up to `role.max_turns` or `TICK_MAX_WALL_SECS` (1200 s):
   - Loop-guard: 3 identical `(tool, args)` in a row → inject "change strategy" user msg.
   - Deadline watchdog: at 75 % of turns with no `post_comment` yet → inject "⚠ DEADLINE … commit + report + exit NOW".
   - Every tool call + tool result → `ticket_event(kind='tool_call')` + structured log line.
8. Writes `tick.end` with `stop_reason` (model_done / max_turns / wall_timeout / llm_error / loop_detected).

Status transitions are agent-driven (`set_status` tool); orchestrator does NOT auto-block stuck tickets (the v4 UI did, which caused false negatives under model latency).

## Tool catalogue (`aiforge_core/runtime/tools.py`)

| Tool | Description | Allowed for |
|---|---|---|
| `search` | bge-m3 + bge-rerank across all tiers | all |
| `read_file` | Read text file (line range) | all |
| `write_file` | Create/overwrite file | doer |
| `edit` | Surgical old_string → new_string (unique-match required) | doer |
| `run_shell` | Bash `-lc`, 120 s default, cwd=worktree | planner, doer, feedback (read-only for feedback) |
| `fetch_url` | GET (20 s), 12 KB body cap | planner, doer |
| `git_commit` | Stage + commit on ticket branch | doer |
| `git_push` | `git push -u origin HEAD` | doer (manual only) |
| `create_child_ticket` | Spawn child under current ticket | supervisor, planner |
| `post_comment` | Append event (`kind=comment`) | all |
| `set_status` | Move ticket through workflow states | all |
| `retain_fact` | Write to `memories` (tier t1/t2/t3, chosen wing) | planner, doer, learner |
| `related_tickets` | Find similar tickets by embedding | all |
| `graph_neighbors` | Call-site map from `graphify-out/graph.json` | planner, doer |
| `kubectl_read` | Safe subset (get/describe/logs/top); auto `--insecure-skip-tls-verify` | planner, doer |
| `mongo_query` | Read-only mongosh via `kubectl exec mongos-0` | planner only |
| `read_claude_memory` | Grep / read `~/.claude/memory/*.md` | all |
| `update_assignee` | Re-route ticket (supervisor's triage action) | supervisor |
| `verdict_pass` | Feedback pass — requires ≥40 char test evidence | feedback |
| `verdict_fail` | Feedback fail — routes back to doer with fixlist | feedback |

## Branch convention

- One branch per parent ticket: `aiforge/<PARENT>-<slug>`.
- Sr Dev + Developer + Fact Extract share it.
- Developer does NOT `git push` automatically; human reviews then pushes.
- Cross-repo tickets: Developer must `git -C <second-repo> checkout -B aiforge/<PARENT>-<slug> origin/master` before committing there.

## Kill switch

Stop all agents:
```bash
for r in supervisor planner doer feedback learner; do
  launchctl bootout gui/$(id -u)/com.aiforge.tick-$r
done
launchctl bootout gui/$(id -u)/com.aiforge.api
launchctl bootout gui/$(id -u)/com.aiforge.reindex-daily
```

Resume: `bash ~/AIForgeCrew/scripts/runtime/install-v5-launchd.sh` + `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aiforge.api.plist`.

Per-ticket stop: `python -m aiforge_core.runtime.cli status ONE-123 --status cancelled`.

## Failure modes and responses

| Symptom | Where to look | Fix |
|---|---|---|
| Tick does nothing | `tail ~/.aiforge/logs/launchd-tick-<role>.log` | usually no `todo` — `list --role` |
| Tool call fails | `ticket_events.metadata.error` | fix args in tool impl or agent prompt |
| LLM 400 (ctx overflow) | `llm.error` event | reduce prompt or raise LM Studio ctx |
| Two ticks collide | `lock.skip` | expected (per-role lock) |
| Worktree prepared at wrong repo | `worktree.prepared` path vs ticket text | child must carry parent's repo — fixed in p13.8 |
| `edit` fails on whitespace | `tool.result` with 0 matches | use `run_shell` + `sed -n '<lines>p' | od -c` to check exact bytes, then retry `edit` |
| Developer blows max_turns | `tick.end.stop_reason=max_turns` | raise `TICK_MAX_TURNS` or tighten prompt schedule |
| API down | `tail ~/.aiforge/logs/api.err.log` | `launchctl kickstart -k gui/$(id -u)/com.aiforge.api` |
| UI loads but empty | browser console + `/api/health` | CORS? wrong port? recheck launchd |

## Model hot-swap

`aiforge_core/runtime/config.py.ROLES` is the source of truth. Edit → `git push` → pull on Mac Studio → `launchctl kickstart -k gui/$(id -u)/com.aiforge.tick-<role>`.

Runtime override via adapter DB is intentionally NOT supported (that's what v4 bootstrap scripts kept regressing).

## 3-month trajectory to full local

- **Now:** Architect = Claude (cloud). Others local.
- **Month 1:** Swap `retain_fact` writer from `store.upsert_memory` (sync) to an async queue so the write doesn't stall the tick.
- **Month 2:** Evaluate a local planner replacement for Architect (qwen3.6-35b or a future MoE). Lower its max_turns to 6.
- **Month 3:** Flip `claude_local` transport to `openai` (LM Studio) — zero-code change, model swap only.
