# AIForgeCrew

Local autonomous coding pipeline. Submit a Java/Python/TS ticket via API; the pipeline plans, edits, compiles, validates, learns, and opens a PR. All inference runs on local `mlx_lm` models. No cloud APIs.

## Verified end-to-end

`ONE-75` — full pipeline pass at 264s wall, all 5 agents fired, PR opened.

```
verdict=pass  duration=264s
per_agent={planner: 1, doer: 4, feedback: 4, learner: 1}
edits=1  compile_green=1  files_changed=1
PR: https://github.com/OneShellSolutions/PosClientBackend/pull/104
```

## Topology

Three-host split. Laptop drives, MS serves models, NUC runs everything else.

| Host | Address | Role | Services |
|---|---|---|---|
| Laptop | — | Orchestrator | SSH client only |
| Mac Studio | `192.168.70.185` | Models only | `mlx_lm` Doer `:1234`, Planner `:1235`, `caffeinate` |
| NUC | `mani@10.10.10.2` (10G link) | Everything else | Postgres 16, Neo4j 5.26 (Docker), `aiforge-api.service`, `aiforge-graph-runner.service`, GenericAgent, Aider, Graphify, source repos, ingest crons |

```
Laptop ─SSH──> NUC :8799 (api)
                  │ ticket claim
                  ▼
              graph-runner (ADK SequentialAgent)
                  ├─ planner (direct LiteLLM → MS :1235)
                  ├─ LoopAgent[doer (GA), feedback (LiteLLM)] → MS :1234
                  └─ learner (LiteLLM → :Fact in NUC Neo4j)
```

| Port | Host | What |
|---|---|---|
| `1234` | MS | `mlx_lm` Doer (Qwen3-Coder-Next-MLX-4bit) |
| `1235` | MS | `mlx_lm` Planner (Qwen3.6-27B-UD-MLX-4bit) |
| `5432` | NUC | Postgres |
| `7474` / `7687` | NUC | Neo4j HTTP / bolt |
| `8799` | NUC | FastAPI (tickets + intervention + memory) |

## Stack

| Component | Notes |
|---|---|
| **ADK 1.31.1** | Google Agent Development Kit. `SequentialAgent` + `LoopAgent`. `DatabaseSessionService` (Postgres). |
| **GenericAgent** (pinned `cd0ce4d`) | Doer's text-protocol agent loop. Sidesteps `mlx_lm` 0.31 native `tool_calls` bug. |
| **Aider RepoMap** | Hot-path code digest in Doer system prompt every call (PageRank-ranked tree-sitter signatures, 1024 tok budget). |
| **Graphify** (`graphifyy`) | Nightly NetworkX graph build → mirror to Neo4j. 25 languages via tree-sitter. |
| **5-layer Neo4j memory** | L0 `:MetaSop`, L2 `:Fact` (vector + fulltext), L3 `:Sop`, L4 `:Session` + `:Turn`, L5 `:File` + `:Symbol`. L1 = ADK Session in-mem. |
| **Postgres 16** | Tickets, ticket events, ADK sessions. |

## GA features wired

| Feature | How AIForge uses it |
|---|---|
| Plan mode (`enter_plan_mode` + checkboxes) | Planner emits `## Steps\n- [ ] step 1`; Doer enters plan mode against `<worktree>/.aiforge/plan.md`; auto-exits when boxes drain. |
| Sub-agent (`--bg`) | `ask_explorer` doer tool spawns a read-only GA subprocess for focused exploration without bloating context. |
| Task intervention (`_stop` / `_keyinfo` / `_intervene`) | `POST /api/tickets/<id>/intervene` writes control files into the live agent's `task_dir`. |
| Runtime param tuning | `POST /api/runtime/session_param` sets `AIFORGE_<ROLE>_<KEY>` env for next agent run. |
| English protocol | `GA_LANG=en` strips Chinese boilerplate from tool-instruction injection. |
| `auto_save_tokens` | Compact `Tools: still active` re-injection saves ~3KB/turn after first. |
| Fuzzy `file_read` suggestions | Auto-prompts nearby paths on miss. |
| `fold_turns` + larger `context_win` | Auto-folds long histories. |
| Multi `code_run` per turn | Multiple shell calls in one turn. |

GA upgrade hardening: single import seam at `aiforge_core/doer/ga_compat.py`. SHA pin in `.aiforge/ga-version.lock`. See [`docs/ga-integration.md`](./docs/ga-integration.md).

## Per-agent rules

Locked in `aiforge_core/agents.yaml`. Five roles: `architect`, `planner`, `doer`, `feedback`, `learner`. Each declares model, backend, tools, `max_turns`, memory scope, termination contract.

Three-layer enforcement: ADK structural filter, GA handler reject, harness pre-flight assert. Loader/validator at `aiforge_core/agents.py`.

| Agent | Backend | Model |
|---|---|---|
| Architect | external | Claude Code (this laptop) |
| Planner | direct LiteLLM | Qwen3.6-27B `:1235` |
| Doer | GA text-protocol | Qwen3-Coder-Next `:1234` |
| Feedback | direct LiteLLM single-shot | Qwen3-Coder-Next |
| Learner | direct LiteLLM single-shot + Neo4j `:Fact` plugin | Qwen3-Coder-Next |

## Memory layout

| Layer | Role | Storage |
|---|---|---|
| L0 META | meta-procedures | NUC Neo4j `:MetaSop` |
| L1 working | per-session | ADK Session (in-mem) |
| L2 facts | global verified | NUC Neo4j `:Fact` + vector + fulltext |
| L3 SOPs | task-specific | NUC Neo4j `:Sop` |
| L4 sessions | every turn auto-remember | NUC Neo4j `:Session` + `:Turn` |
| L5 code | AST + Aider + Graphify | NUC Neo4j `:File` + `:Symbol` (+ Aider SQLite hot cache) |

## Quick start

Submit:
```bash
curl -X POST http://10.10.10.2:8799/api/tickets \
  -H 'Content-Type: application/json' \
  -d '{"title":"Add log to MyController.save",
       "body":"## Files\n- repo/path/MyController.java\n## Acceptance\n1. log.info...",
       "assignee_role":"developer",
       "project":"PosClientBackend"}'
```

Watch:
```bash
curl -s http://10.10.10.2:8799/api/tickets/<id>
tail -f /home/mani/.aiforge/logs/graph-runner.err   # on NUC
```

Pull facts (Neo4j Cypher at `bolt://10.10.10.2:7687`):
```cypher
MATCH (f:Fact) WHERE f.ticket_id = $id RETURN f ORDER BY f.created_at DESC
```

Steer a live agent:
```bash
curl -X POST http://10.10.10.2:8799/api/tickets/<id>/intervene \
  -d '{"kind":"intervene","body":"focus on src/main only, skip tests"}'
```

Tune model param mid-run for next-tick:
```bash
curl -X POST http://10.10.10.2:8799/api/runtime/session_param \
  -d '{"role":"doer","key":"temperature","value":0.05}'
```

## Repo layout

```
aiforge_core/
  agents.yaml                  per-agent contracts (source of truth)
  agents.py                    loader + validator
  doer/
    ga_runner.py               GenericAgent text-protocol path
    ga_compat.py               single import seam for GA upgrades
    orchestrator_bridge.py     backend dispatch
    scope_guard.py             write-allowlist parser
    acceptance_gate.py
  planner/
    ga_runner.py               direct-LiteLLM checkbox-plan emitter
    agent.py                   smolagents fallback (legacy)
  index/
    aider_map.py               Aider RepoMap wrapper
    graphify_loader.py         NetworkX → Neo4j mirror
    treesitter_ingest.py       direct AST → Neo4j
  memory/
    schema.py                  Neo4j constraints + vector + fulltext indexes
    cypher_templates.py        lookup_symbol, find_callers, find_definition
  eval/
    rule_checker.py            agents.yaml rule enforcement
  runtime/
    adk_runner.py              ADK ticket-claim daemon
    adk_workflow.py            SequentialAgent[Planner, LoopAgent[Doer,Feedback], Learner]
    api.py                     FastAPI :8799 (tickets + intervention + memory)
    agent_config.py            role → model + base_url
  legacy/                      v4 modules (LangGraph + RAG); pending removal
docs/
  architecture.md              topology + per-agent rules
  stack.md                     stack reference
  agent-rules.md               per-agent rules + 3-layer enforcement
  ga-integration.md            GA upgrade checklist + SHA pin protocol
evals/
  fixtures/F1..F7c.yaml        Java Spring Boot test fixtures
  results/                     run JSONs
scripts/
  evals/
    run_genericagent_eval.py   GA chain harness
    run_eval.py                ticket harness
    run_planner_eval.py        EVAL-1b scaffold (planner backend comparison)
    report.py                  aggregator
  runtime/
    com.aiforge.mlx-doer.plist     MS launchd
    com.aiforge.mlx-planner.plist  MS launchd
    graphify-nightly.sh            cron
    ga-pin.sh                      GA SHA pin / check / show
.aiforge/
  ga-version.lock              pinned GA SHA + date
```

## Operational

| Action | Command |
|---|---|
| Restart ADK runner (NUC) | `systemctl --user restart aiforge-graph-runner.service` |
| Restart `mlx_lm` Doer (MS) | `launchctl kickstart -k gui/501/com.aiforge.mlx-doer` |
| Restart `mlx_lm` Planner (MS) | `launchctl kickstart -k gui/501/com.aiforge.mlx-planner` |
| Tail logs | `tail -f /home/mani/.aiforge/logs/graph-runner.err` |
| Pin GA SHA | `./scripts/runtime/ga-pin.sh` |
| Verify GA pin | `./scripts/runtime/ga-pin.sh --check` |
| Run eval chain | `.venv/bin/python scripts/evals/run_genericagent_eval.py --chain F7a,F7b,F7c` |

## Docs

| Read | For |
|---|---|
| [`docs/ticket-flow.md`](./docs/ticket-flow.md) | Visual end-to-end flow (mermaid diagrams) |
| [`docs/agents.md`](./docs/agents.md) | Five agents + ADK orchestrator wiring |
| [`docs/memory.md`](./docs/memory.md) | 5-layer memory model + per-agent access |
| [`docs/architecture.md`](./docs/architecture.md) | Topology + per-agent rules |
| [`docs/stack.md`](./docs/stack.md) | Tooling reference |
| [`docs/agent-rules.md`](./docs/agent-rules.md) | Per-agent rules + 3-layer enforcement |
| [`docs/ga-integration.md`](./docs/ga-integration.md) | GA upgrade checklist + SHA pin protocol |
| [`docs/runbook.md`](./docs/runbook.md) | Ops runbook |

## License

MIT
