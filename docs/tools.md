# ga_tools catalogue

24 KISS modules under `aiforge_core/doer/ga_tools/`. Each exports
`SCHEMA` (OpenAI-function shape) + `handle()` (pure logic). GA
handler thin-wraps in `do_<tool>`.

## Edit

| Tool | File | What |
|---|---|---|
| `file_patch` | (GA built-in + counters) | SEARCH/REPLACE block edit · plan_mode-guarded · post_edit hook |
| `file_write` | (GA built-in + counters) | Full-file write · plan_mode-guarded |
| `bulk_edit` | `bulk_edit.py` | Atomic multi-file edit · rollback on first failure |
| `aider_blocks` | `aider_blocks.py` | Aider's SEARCH/REPLACE format parser |
| `java_refactor` | `java_refactor.py` | OpenRewrite recipe via mvn rewrite:run · plan_mode-guarded |

## Read

| Tool | File | What |
|---|---|---|
| `file_read` | (GA built-in + ReadTracker) | Lined excerpt · per-run cache |
| `glob` | `glob.py` | Pattern listing (Claude Code style) |
| `grep` | `grep.py` | Ripgrep wrapper |
| `batch` | `batch.py` | Parallel fan-out of read tools |

## Shell / build

| Tool | File | What |
|---|---|---|
| `bash` | `bash.py` | Persistent shell session · sticky cwd |
| `code_run` | (GA built-in + caps) | mvn capped at 2/ticket · grep/find/ls forbidden · sandbox-wrappable |
| `lint` | `lint.py` | Repo lint cmd from standards (env or :Repo) |
| `tests` | `tests.py` | Repo test cmd from standards · surefire parser |

## Search

| Tool | File | What |
|---|---|---|
| `search_memory` | (chat) | Hybrid Neo4j retrieval |
| `unified_memory_query` | `memory/unified_query.py` | One-call fan-out: memory + ticket_brief + related + sym_lookup + find_doc + docs |
| `ask_explorer` | `ga_runner.py` | Spawn 1..4 read-only sub-agents, parallel, summary return |
| `web_search` | `web_search.py` | Gemini grounded search (when AIFORGE_GOOGLE_API_KEY set) |

## Plan / track

| Tool | File | What |
|---|---|---|
| `enter_plan_mode` / `exit_plan_mode` | `plan_mode.py` | Read-only think gate · blocks writes until exit · **`exit_plan_mode` auto-parses numbered/bulleted plan into checklist via `todos.write`** |
| `todo_write` / `todo_check` | `todos.py` | In-loop checklist · pretty render injected into prompt · auto-seeded by `exit_plan_mode` |

## Sub-agent

| Tool | File | What |
|---|---|---|
| `dispatch_subagent` | `subagent.py` | Isolated GA loop · whitelisted read-only tools · summary-only return · cap 1/turn |

## Quality

| Tool | File | What |
|---|---|---|
| `edit_verify` | `edit_verify.py` | Git-diff banner appended after each landed patch |
| `undo` | `undo.py` | Roll back last edit (single file or whole batch) |
| `conventions` | `conventions.py` | `.aiforge/CONVENTIONS.md` injector |

## Safety

| Tool | File | What |
|---|---|---|
| `secrets` | `secrets.py` + `secrets_cli.py` | gitleaks → trufflehog → regex heuristic · builtin pre_commit hook · `block:true` |
| `sandbox` | `sandbox.py` | firejail / docker wrap for code_run · `AIFORGE_DOER_SANDBOX=firejail\|docker` |
| `readonly` | `readonly.py` | Pin files visible-only |
| `tokens` | `tokens.py` | Per-ticket per-role token tracker |

## Optimisation

| Tool | File | What |
|---|---|---|
| `compaction` | `compaction.py` | Middle-out history elision near 0.8×context_win |
| `repo_config` | `repo_config.py` | `.aiforge/aiforge.conf.yml` per-worktree overrides |

## Hook events (full taxonomy)

| Event | Fires when | Default builtin |
|---|---|---|
| `pre_tool` | Before any do_<tool> | (none) |
| `post_tool` | After any do_<tool>, with wall_ms | perf emit_step |
| `pre_file_read` / `post_file_read` | file_read dispatch | perf emit_step |
| `pre_file_write` / `post_file_write` | file_patch / file_write / bulk_edit | perf emit_step |
| `pre_search` / `post_search` | glob / grep / search_memory / unified_memory_query | perf emit_step |
| `pre_llm` / `post_llm` | session.raw_ask round-trip | (planned) |
| `post_edit` | landed patch | user hooks via `.aiforge/hooks.yml` |
| `post_compile` | mvn BUILD SUCCESS | user hooks |
| `post_test` | tests command done | user hooks |
| `pre_commit` | before git commit | **builtin: secret-scan (`block:true`)** |
| `agent_start` / `agent_end` | Doer / Planner lifecycle | (planned) |

### Hook config (`.aiforge/hooks.yml`)

```yaml
- event: post_edit
  run: ./scripts/format.sh
  timeout: 30
  block: false   # true = on non-zero exit, abort the next agent turn

- event: post_compile
  run: ./scripts/spotless-check.sh
  timeout: 60

- event: pre_commit
  run: gitleaks protect --staged
  block: true
```

Each hook gets these env vars:

| Env | What |
|---|---|
| `AIFORGE_HOOK_EVENT` | event name |
| `AIFORGE_HOOK_PAYLOAD` | JSON `{tool, args_keys, role, ...}` |
| (rest of operator env via systemd EnvironmentFile) |

## Performance metrics

```
GET /api/runtime/perf
{
  "rows": [
    {"event": "post_search", "name": "unified_memory_query",
     "count": 12, "total_ms": 18204, "max_ms": 4521},
    {"event": "post_file_read", "name": "file_read",
     "count": 27, "total_ms": 1245, "max_ms": 91},
    ...
  ]
}
```

NDJSON tail: `~/.aiforge/perf.ndjson` (one line per step). Disable
via `AIFORGE_PERF_NDJSON=0`. Reset in-memory aggregator via
`?reset=true` on the endpoint.

## Adding a new tool

KISS recipe:

1. New file `aiforge_core/doer/ga_tools/<concern>.py`.
2. Export `SCHEMA` (OpenAI-function shape) + `handle(cwd, args, ...)` pure logic.
3. Update `ga_tools/__init__.py` import + `__all__`.
4. Wire `do_<concern>` thin wrapper in `doer/ga_runner.py`.
5. Env-flag-gate registration in the schema list (default off until smoke).
6. Document in this file.

That's it. Same pattern as 24 existing tools.
