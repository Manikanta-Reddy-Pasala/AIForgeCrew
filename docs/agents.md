# Agents

## Five roles + chat

```
                        ticket
                          │
                          ▼
┌────────────┐    ┌─────────────┐    ┌──────────┐    ┌───────────┐    ┌──────────────┐    ┌─────────┐
│ Architect  │───►│   Planner   │───►│   Doer   │───►│ Feedback  │───►│ Integration  │───►│ Learner │
│ (Claude    │    │ smolagents  │    │ GA loop  │    │ verdict   │    │ smoke runner │    │ distill │
│  Code, ext)│    │ + write_plan│    │ + tools  │    │ + retry   │    │ + gh_pr      │    │ → memory│
└────────────┘    └─────────────┘    └──────────┘    └───────────┘    └──────────────┘    └─────────┘
                                          │ ◄────────────┘
                                          │ loop on compile_red
                                          ▼
                                     hooks · perf · cost
```

## Per-role contract

| Role | Tools | Memory access | Writes | Max wall | Backend |
|---|---|---|---|---|---|
| **Architect** | none (external) | reads L2/L3 | tickets only | n/a | Claude Code |
| **Planner** | lookup_repo, search_memory, grep_repos, read_file, extract_signatures, write_plan, create_child_ticket | T2 facts auto-injected, T3 SOPs | plan.md, child tickets | 8 min | smolagents CodeAgent · `AIFORGE_PLANNER_BACKEND=genericagent` swaps |
| **Doer** | 25 ga_tools (see tools.md) | T2 + T5 (Aider) auto, T4 via tool | files in scope only (ScopeGuard) | 25 min | GA text-protocol |
| **Feedback** | targeted_fixlist | reads doer counters + last_compile_error | retry verdict | 2 min | LLM |
| **Learner** | distill, retain_fact | reads doer outcome | T3 facts | 2 min | LLM |
| **Chat** | unified_memory_query, ops_<*>, graph_rag MCP allowlist, final_answer | T1..T5 + external docs | T3 chat_qa fact (auto-retain) | 1 min | GA · ollama_cloud default |

## Tool taxonomy (25 ga_tools)

```
EDIT          ── file_patch · file_write · bulk_edit · java_refactor
READ          ── file_read · glob · grep · batch
SHELL         ── bash · code_run (mvn-capped, sandbox-wrappable)
SEARCH        ── search_memory · unified_memory_query · ask_explorer · web_search
PLAN          ── enter_plan_mode · exit_plan_mode · todo_write · todo_check
SUB-AGENT     ── dispatch_subagent (isolated GA loop, summary-only return)
QUALITY       ── lint · tests · undo · edit_verify
SAFETY        ── secrets (pre_commit) · sandbox (firejail/docker)
ANALYSIS      ── conventions · readonly · tokens · repo_config
HOOKS         ── post_edit · post_compile · post_test · pre_commit · pre/post_tool · pre/post_search · pre/post_llm
OPS MCPs      ── ops_mongo_* · ops_k8s_* · ops_tekton_* · ops_tally_*  (102 tools/tier)
```

## Decision matrix — backend per role

| Decision | Why |
|---|---|
| Planner = smolagents CodeAgent | EVAL-1 (2026-04-23): wrote plan 3/3 vs ToolCallingAgent 1/3 |
| Doer = GA text-protocol | mlx-lm 0.31 drops native tool_calls; text-protocol JSON-block parse works |
| Chat = GA + ollama_cloud | Cloud streams `reasoning` chunks; GA `stream=False` cfg avoids SSE re-assembly bug |
| Feedback = LLM (no agent) | Single decision per turn; agent overhead unwarranted |
| Learner = templated + LLM | Deterministic distill template; LLM only for prose summary |

## Per-agent activity

### Planner

```
ticket body ──► lookup_repo(project)       (Neo4j :Repo)
            ──► search_memory(title)       (T2 + T3 hits)
            ──► grep_repos(symbol)         (ripgrep across ~/codeRepo)
            ──► read_file(candidates)      (confirm targets)
            ──► extract_signatures(target) (method sigs)
            ──► write_plan(files, steps, signatures, pitfalls)
            ──► final_answer
```

### Doer

```
plan + allowed_files
   │
   ├─ standards prompt block (Neo4j :Repo)
   ├─ Aider RepoMap (PageRank, mtime-cache)
   ├─ Graphify neighbour symbols
   │
   ▼
GA agent_runner_loop  (max 40 turns / 25 min)
   │
   ├─ optional: enter_plan_mode → read-only think → exit_plan_mode(plan)
   │              └─► auto-seeds checklist via todos.write (numbered
   │                  /bulleted lines parsed into items)
   ├─ todo_check flips item status as work progresses
   │
   ├─ READ:  file_read · glob · grep · batch · ask_explorer
   ├─ EDIT:  file_patch · bulk_edit · java_refactor (Plan Mode gates these)
   ├─ TEST:  code_run (mvn cap=2) · tests · lint
   ├─ DELEGATE: dispatch_subagent (isolated)
   │
   └─ on landed edit: post_edit hook (.aiforge/hooks.yml + builtin secrets pre_commit)
   └─ on BUILD SUCCESS: post_compile hook
   └─ counters bumped: edit_block_ok, compile_green
```

### Chat

```
POST /api/chat/ask
   │
   ├─ normalize query
   ├─ GA loop (max 12 turns)
   │    │
   │    ├─ unified_memory_query  (preferred, 6 sources merged)
   │    ├─ search_memory | ticket_brief | sym_lookup | find_doc | ops_<*>
   │    └─ final_answer
   │
   ├─ fallback A: last successful tool_result text
   ├─ fallback B: last assistant text (skip 未知工具/<thinking>)
   ├─ Memory.retain_fact(t3, patterns/chat-auto, kind=chat_qa)
   ├─ cost.record_call
   └─ return {answer, hits, tools_called, auto_retained_id}
```

## Env knobs per agent

| Knob | Default | Effect |
|---|---|---|
| `AIFORGE_PLANNER_BACKEND` | `code` | `code` (smolagents CodeAgent) · `toolcalling` (smolagents TC) · `genericagent` (GA text-protocol) |
| `AIFORGE_DOER_PLAN_MODE` | 0 | enter/exit_plan_mode tools |
| `AIFORGE_DOER_TODOS` | 0 | todo_write/todo_check |
| `AIFORGE_DOER_SUBAGENT` | 0 | dispatch_subagent |
| `AIFORGE_DOER_HOOKS` | 0 | .aiforge/hooks.yml + builtin secret-scan |
| `AIFORGE_DOER_COMPACT` | 0 | middle-out history elision near 0.8×ctx |
| `AIFORGE_DOER_SANDBOX` | off | firejail / docker |
| `AIFORGE_DOER_OPS_MCP` | 0 | ops MCPs in Doer |
| `AIFORGE_CHAT_OPS_MCP` | 1 | ops MCPs in Chat |
| `AIFORGE_DOER_AUTOLEARN` | 1 | distill on Doer done |
| `AIFORGE_CHAT_AUTORETAIN` | 1 | retain_fact on Chat answer |
| `AIFORGE_PERF_NDJSON` | 1 | append per-step wall_ms to ~/.aiforge/perf.ndjson |
| `AIFORGE_OTEL_ENABLED` | 0 | OpenTelemetry export to OTLP |
