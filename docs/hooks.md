# Hooks

Per-step automation + safety + perf metrics. Mirrors Claude Code's
hooks system + adds telemetry.

## Pipeline

```
do_<tool>(args)
   │
   ├─ tool_before_callback ──► perf t0 stash, plan_mode guard, deny-list
   │                              │
   │                              └─ pre_tool / pre_file_* / pre_search hooks
   │
   ├─ do_<tool> body
   │
   ├─ tool_after_callback ──► perf wall_ms, hooks.emit_step
   │                              │
   │                              ├─ post_tool / post_file_* / post_search hooks
   │                              ├─ ndjson append (~/.aiforge/perf.ndjson)
   │                              └─ in-memory aggregator (/api/runtime/perf)
   │
   └─ specialised:
        post_edit  ─► .aiforge/hooks.yml (after file_patch/file_write/bulk_edit)
        post_compile ─► .aiforge/hooks.yml (after mvn BUILD SUCCESS)
        pre_commit ─► **builtin: secret-scan** + .aiforge/hooks.yml
```

## Event taxonomy

| Group | Events | Used for |
|---|---|---|
| **Tool dispatch** | pre_tool · post_tool | universal perf, allow/deny |
| **File I/O** | pre_file_read · post_file_read · pre_file_write · post_file_write | per-file gates, audit logs |
| **Search** | pre_search · post_search | rate-limit, cache warm |
| **LLM** | pre_llm · post_llm | token budget, prompt cache, cost |
| **Edit cycle** | post_edit · post_compile · post_test · pre_commit | format / lint / security gates |
| **Agent lifecycle** | agent_start · agent_end | tracing, billing, cleanup |

## Configuration

`.aiforge/hooks.yml` at worktree root. KISS: shell commands only,
no Python plugins.

```yaml
- event: post_edit
  run: npx prettier --write .
  timeout: 30
  block: false

- event: post_compile
  run: ./scripts/spotbugs.sh
  timeout: 120
  block: false

- event: pre_commit
  run: gitleaks protect --staged
  block: true
```

| Field | Default | What |
|---|---|---|
| `event` | required | one of taxonomy above |
| `run` | required | shell command, cwd = worktree |
| `timeout` | 30 | seconds before SIGTERM |
| `block` | false | true → non-zero exit aborts next agent turn |

## Builtin hooks (always-on)

| Hook | Toggle | What |
|---|---|---|
| Secret-scan pre_commit | `AIFORGE_DOER_SECRET_SCAN=0` to disable | gitleaks → trufflehog → regex heuristic chain · `block:true` |
| Perf step recorder | `AIFORGE_PERF_NDJSON=0` to disable ndjson | aggregator always on |

## Environment exposed to hooks

| Var | Source | Example |
|---|---|---|
| `AIFORGE_HOOK_EVENT` | dispatcher | `post_edit` |
| `AIFORGE_HOOK_PAYLOAD` | dispatcher (JSON) | `{"tool":"file_patch","args_keys":["path","old_content","new_content"]}` |
| ...full operator env | systemd EnvironmentFile | `OLLAMA_CLOUD_API_KEY`, `AIFORGE_DSN`, ... |

## Perf snapshot

```
$ curl http://nuc:8799/api/runtime/perf
{
  "rows": [
    {"event":"post_search","name":"unified_memory_query","count":12,"total_ms":18204,"max_ms":4521},
    {"event":"post_file_read","name":"file_read","count":27,"total_ms":1245,"max_ms":91},
    {"event":"post_tool","name":"ops_k8s_list_pods","count":3,"total_ms":7841,"max_ms":3120},
    {"event":"post_file_write","name":"file_patch","count":5,"total_ms":410,"max_ms":120}
  ]
}
```

ASCII waterfall (slowest first):

```
unified_memory_query  ████████████████████ 18204 ms  (12 calls, max 4521)
ops_k8s_list_pods     ████████             7841 ms  (3 calls, max 3120)
file_read             █                    1245 ms  (27 calls, max 91)
file_patch            ▌                    410 ms   (5 calls, max 120)
```

## NDJSON stream

`~/.aiforge/perf.ndjson` — one row per step. Cron-friendly for
Grafana / Loki ingest:

```
{"event":"post_search","name":"unified_memory_query","wall_ms":1820,"extra":{"role":"chat"}}
{"event":"post_tool","name":"ops_k8s_list_pods","wall_ms":3120,"extra":{"args_keys":["namespace"]}}
{"event":"post_file_read","name":"file_read","wall_ms":47,"extra":{"args_keys":["path"]}}
```

Path overridable via `AIFORGE_PERF_NDJSON_PATH`.

## Per-step decision matrix

| Want | How |
|---|---|
| Format on every save | `post_edit` + `prettier --write` |
| Lint gate before commit | `pre_commit` + `eslint .` + `block:true` |
| Block secrets pre-push | already builtin (`AIFORGE_DOER_SECRET_SCAN=1`) |
| Trace every search call | already builtin (perf recorder) |
| Slow-query alert | `post_search` + custom shell (`if wall_ms > 5000 ...`) |
| Per-tool $ budget | `post_tool` + `cost rollup` query |
| Sub-agent quota | `pre_tool` + check `AIFORGE_HOOK_PAYLOAD.tool == "dispatch_subagent"` + counter file |

## Wiring a new event

KISS recipe:

1. Add event name to `_VALID_EVENTS` in `ga_tools/hooks.py`.
2. Add `hooks.emit_step(event=..., name=..., wall_ms=...)` call at the
   right place in `ga_runner.py` (handler method or callback).
3. Document in this file.

No new file required for new events — taxonomy is data, not code.
