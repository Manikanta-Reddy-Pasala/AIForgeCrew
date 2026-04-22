# AIForgeCrew Runbook

Ops guide for the LangGraph-based autonomous dev-team pipeline. Single graph-runner process; one launchd plist; Postgres as the source of truth.

---

## Stack at a glance

| Layer | Component | Port | Owner |
|-------|-----------|------|-------|
| Inference | LM Studio | 1234 | `lms server` |
| Embeddings | bge-m3 sidecar | 8764 | launchd |
| Rerank | bge-reranker-v2-m3 sidecar | 8765 | launchd |
| Storage | Postgres + pgvector (`aiforge` DB) | 5432 | homebrew postgresql |
| Orchestration | LangGraph graph-runner | — | launchd `com.aiforge.graph-runner` (60 s) |
| Watchdogs | pg-watchdog, git-pull, file-indexer, reindex-daily | — | launchd |

---

## 1. Daily commands

### Tail logs

```bash
# Graph-runner raw output
tail -f ~/.aiforge/logs/graph-runner.log

# Structured ndjson (all roles combined)
tail -f ~/.aiforge/logs/orchestrator-*.ndjson \
  | jq -c '{ts, role, ticket, event, tool, dur_ms}'

# Last 20 graph runner events
tail -20 ~/.aiforge/logs/graph-runner.log
```

### Process check

```bash
# Confirm plist is loaded
launchctl list | grep aiforge

# LM Studio loaded models
lms ps

# Postgres
pg_isready -d aiforge
```

### Common psql queries

```sql
-- Open tickets
SELECT identifier, assignee_role, status, priority, updated_at
FROM tickets
WHERE status NOT IN ('done', 'cancelled')
ORDER BY updated_at DESC;

-- Recent ticket events for ONE-123
SELECT created_at, agent_role, kind, left(body, 200)
FROM ticket_events
WHERE ticket_id = (SELECT id FROM tickets WHERE identifier = 'ONE-123')
ORDER BY created_at;

-- Feedback verdicts today
SELECT t.identifier, te.metadata->>'feedback_verdict' AS verdict, te.created_at
FROM ticket_events te
JOIN tickets t ON t.id = te.ticket_id
WHERE te.agent_role = 'feedback'
  AND te.created_at > now() - interval '24 hours';

-- LangGraph checkpoint for a ticket
SELECT thread_id, checkpoint_id, created_at
FROM checkpoints
WHERE thread_id = 'ONE-123'
ORDER BY created_at DESC
LIMIT 5;

-- Recent memories written
SELECT wing, left(content, 120), tier, created_at
FROM memories
ORDER BY created_at DESC
LIMIT 10;
```

---

## 2. Unsticking a ticket

### Ticket stuck in in_progress

The graph-runner crashed mid-run, or the process was killed. The ticket remains `in_progress` indefinitely since no agent will re-claim it.

```sql
-- Reset to todo so the next poll picks it up
UPDATE tickets
SET status = 'todo', updated_at = now()
WHERE identifier = 'ONE-123';
```

### Worktree cleanup

If the worktree is in a dirty state after a crash:

```bash
REPO=~/codeRepo/<project>
TICKET=ONE-123

# Remove the worktree (git will complain if it has uncommitted changes — add --force if needed)
git -C "$REPO" worktree remove "$REPO/.aiforge-worktrees/$TICKET" --force

# Delete the branch if you want a clean retry
git -C "$REPO" branch -D "aiforge/$TICKET-<slug>"
```

After cleanup, reset the ticket to `todo` (SQL above). The next poll will recreate the worktree from `origin/<default-branch>`.

### Ticket blocked by scope violation

Feedback returned `verdict=scope_violation`. Graph ended. The ticket is in `blocked` status.

1. Review the ticket's `## Files` allowlist — it may be missing a file the Doer needs to edit.
2. Edit the ticket body to add the missing path.
3. Reset: `UPDATE tickets SET status = 'todo' WHERE identifier = 'ONE-123';`

### Ticket looping on fail

Feedback has failed twice (`feedback_fail_count >= 2`). Graph ended, ticket is `blocked`.

1. Check ticket events for the `fixlist` in the feedback comments.
2. Either fix the ticket body (clarify acceptance criteria) or manually edit the worktree and reset to `in_review`:

```sql
UPDATE tickets SET status = 'in_review', updated_at = now()
WHERE identifier = 'ONE-123';
```

---

## 3. Model management

### Required models (must be loaded or loadable in LM Studio)

| Role | Model | Notes |
|------|-------|-------|
| Supervisor / Feedback | `gemma-4-26b-a4b-it` | Set via `AIFORGE_SUPERVISOR_MODEL` + `AIFORGE_FEEDBACK_MODEL` in plist |
| Planner | `openai/gpt-oss-20b` | Default in `config.py` |
| Doer | `qwen3-coder-next` | Default in `config.py`; the primary hot model |
| Learner | `openai/gpt-oss-20b` | Set via `AIFORGE_LEARNER_MODEL` in plist |

### Health checks

```bash
# LM Studio server responding
curl -s http://127.0.0.1:1234/v1/models | jq '.data[].id'

# bge-m3 embed sidecar
curl -s http://127.0.0.1:8764/health

# bge-reranker-v2-m3 sidecar
curl -s http://127.0.0.1:8765/health
```

### Load a model manually

```bash
lms load gemma-4-26b-a4b-it --context-length 16384
lms load qwen3-coder-next --context-length 131072
```

### Hot-swap a model

Edit `aiforge_core/runtime/config.py` (defaults) or the plist env vars, then:

```bash
git push                          # on laptop / dev machine
ssh mac-studio "cd ~/AIForgeCrew && git pull"
launchctl kickstart -k gui/$(id -u)/com.aiforge.graph-runner
```

---

## 4. Graph-runner troubleshooting

### Reading log events

```bash
tail -f ~/.aiforge/logs/graph-runner.log
tail -f ~/.aiforge/logs/orchestrator-*.ndjson | jq -c '{ts,event,ticket,stop_reason,verdict}'
```

### Key log events and what they mean

| Event | Meaning | Action |
|-------|---------|--------|
| `graph_runner.start` | Ticket claimed, graph invoked | Normal |
| `graph_runner.done stop_reason=done` | Happy path, ticket marked done | None |
| `graph_runner.done stop_reason=blocked` | Scope violation or loop-break | Check fixlist; reset ticket if fixable |
| `graph_runner.done verdict=scope_violation` | Doer wrote outside `## Files` allowlist | Extend allowlist in ticket body |
| `graph_runner.exception` | Unhandled exception in a node | Check `graph-runner.err` for traceback |
| `smolagents.no_changes` | Doer called final_answer with empty diff | Ticket comment posted; graph ends; re-queue |
| `smolagents.scope_violation` | Doer tried to write a disallowed file | `ScopeViolation` caught; feedback_fail_count++ |
| `supervisor.route assignee=<role>` | Supervisor resolved role, forwarding | Normal |
| `tick.idle` | No todo tickets at poll time | Normal |

### Graph ends without reaching learner

Expected when:
- `verdict=scope_violation` (Doer touched files outside `## Files`)
- `feedback_fail_count >= 2` (two consecutive fail verdicts)
- `stop_reason=done/blocked` emitted by any node

Check `ticket_events` for the feedback comment with `fixlist` to understand why.

### `graph_runner.exception` with LangGraph checkpoint error

If `PostgresSaver` fails (e.g. checkpoint tables missing), the graph falls back to no checkpointing and continues. If it fails fatally:

```bash
psql aiforge < db/migrations/2026-04-23-langgraph-checkpoints.sql
```

---

## 5. RAG troubleshooting

### Embed sidecar down

`retrieve_for_role_li` catches sidecar errors per-tier. Vector retrieval for that tier returns `[]`; BM25 results still contribute. The node continues without crashing. Watch for log lines:

```
vector retrieve failed: ...
```

Restart the sidecar and verify:

```bash
curl -s http://127.0.0.1:8764/health
```

### Rerank sidecar down

`_rerank` in `aiforge_core/rag/retriever.py` catches sidecar errors and falls back to the RRF-fused order. Retrieval still works, just unranked. Watch for:

```
rerank sidecar failed (...); falling back to RRF order
```

### `data_memories` error (LlamaIndex PGVectorStore relic)

If someone swaps `rag/retriever.py` to use `LlamaIndex PGVectorStore`, it will fail with a table-not-found error because LlamaIndex hardcodes a `data_` prefix (looking for `data_memories` instead of `memories`). The fix is to revert to the `store_v2`-direct path — do not use `PGVectorStore` with this schema.

### Verify retrieval is working

```bash
python - <<'EOF'
from aiforge_core.rag.retriever import retrieve_for_role_li
hits = retrieve_for_role_li(None, "doer", "pagination controller", None)
print(len(hits), "hits")
for h in hits[:3]:
    print(h.tier, h.score, h.text[:80])
EOF
```

---

## 6. Canary procedure

Use this to verify the full pipeline after any infrastructure change.

### Insert a canary ticket

```sql
INSERT INTO tickets (identifier, title, body, status, priority, assignee_role, created_at, updated_at)
VALUES (
  'ONE-CANARY-' || floor(random()*9000+1000)::text,
  'Canary: verify graph pipeline',
  E'## Files\n- README.md\n## Acceptance\n- Add a comment line to README.md\n- Compile is skipped (non-Java repo)',
  'todo',
  'medium',
  'doer',
  now(),
  now()
);
```

### Kick the runner

```bash
# Force an immediate tick (without waiting for the 60 s interval)
launchctl kickstart gui/$(id -u)/com.aiforge.graph-runner
```

### Watch events

```bash
tail -f ~/.aiforge/logs/graph-runner.log &
psql aiforge -c "
  SELECT created_at, agent_role, kind, left(body, 120)
  FROM ticket_events
  WHERE ticket_id = (SELECT id FROM tickets WHERE identifier LIKE 'ONE-CANARY-%' ORDER BY created_at DESC LIMIT 1)
  ORDER BY created_at;"
```

### Expected sequence

```
graph_runner.start
supervisor.route → doer_node
smolagents.start
smolagents.done  (files_changed >= 1)
feedback: verdict=pass   (or fail if acceptance not met)
learner: DIGEST written
graph_runner.done stop_reason=done
```

End-to-end wall time for a compile-green path: ~2 minutes.

---

## 7. Rollback plan

There is no automated rollback. The legacy per-role stack was deleted in commit 31a2bf8. If the LangGraph pipeline is broken and must be abandoned:

1. Find the last pre-migration commit: `git log --oneline | grep -i "before migration"` or check `docs/migration/2026-04-22-langgraph-llamaindex-smolagents.md` for the reference commit.
2. `git revert <commit-range>` or `git checkout <pre-migration-sha> -- .` (the latter is destructive; commit the result).
3. Reinstall the old plists. The old plist names were: `com.aiforge.tick-supervisor`, `com.aiforge.tick-planner`, `com.aiforge.tick-planner-b`, `com.aiforge.tick-doer`, `com.aiforge.tick-doer-b`, `com.aiforge.tick-feedback`, `com.aiforge.tick-learner`. These are gone from the repo; you would need to recover them from git history.
4. Restore `aiforge_core/runtime/feature_flags.py` and the old `tickets.claim_next(role)` signature from git history.

**This is intentionally painful.** The migration is one-way. Fix forward when possible.

---

## 8. Kill switch

```bash
# Stop the graph-runner (tickets in todo stay todo; in_progress may need manual reset)
launchctl bootout gui/$(id -u)/com.aiforge.graph-runner

# Stop everything aiforge
for label in com.aiforge.graph-runner com.aiforge.pg-watchdog com.aiforge.git-pull \
             com.aiforge.file-indexer com.aiforge.reindex-daily; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
done

# Resume
bash ~/AIForgeCrew/scripts/runtime/install-launchd.sh
```

Per-ticket stop (prevents re-claim without touching infra):

```sql
UPDATE tickets SET status = 'cancelled', updated_at = now()
WHERE identifier = 'ONE-123';
```
