# AIForgeCrew

Local autonomous coding pipeline. Submit a Java/Python/TS ticket via API; pipeline plans, edits, compiles, validates, and learns. All inference runs on local `mlx_lm` models. No cloud APIs.

## Topology (v5)

Three-host split. Laptop drives, MS serves models, NUC runs everything else.

| Host | Address | Role | Services |
|---|---|---|---|
| Laptop | — | Orchestrator | SSH client only. Submits tickets, monitors. |
| Mac Studio | `192.168.70.185` | Models only | `mlx_lm` Doer `:1234` (Qwen3-Coder-Next-MLX-4bit), `mlx_lm` Planner `:1235` (Qwen3.6-27B-UD-MLX-4bit), launchd plists, `caffeinate` |
| NUC | `mani@10.10.10.2` (10G link from MS) | Everything else | Postgres 16, Neo4j 5.26 (Docker), `aiforge-api.service`, `aiforge-graph-runner.service`, GenericAgent, Aider, Graphify, source repos, ingest cron timers |

```
Laptop ─SSH──> NUC :8799 (api)         NUC :7687 (neo4j bolt)
                  │                       ▲
                  │ ticket claim          │ memory R/W
                  ▼                       │
              graph-runner ──HTTP──> MS :1234 (Doer mlx_lm)
                          └─HTTP──> MS :1235 (Planner mlx_lm)
```

| Port | Host | What |
|---|---|---|
| 1234 | MS | `mlx_lm` Doer (OpenAI-compat) |
| 1235 | MS | `mlx_lm` Planner |
| 5432 | NUC | Postgres |
| 7474 / 7687 | NUC | Neo4j HTTP / bolt |
| 8799 | NUC | FastAPI tickets + events |

## Key components

| Component | Status | Notes |
|---|---|---|
| ADK (Google Agent Development Kit) | Phase 6, scaffolded | Replaces LangGraph; LangGraph still in production |
| GenericAgent for Doer | Production | Text-protocol; sidesteps `mlx_lm` 0.31 native `tool_calls` bug. 13/13 Java pass |
| smolagents `CodeAgent` for Planner | Production | Until EVAL-1b says swap |
| direct LiteLLM | Production | Feedback + Learner |
| Aider RepoMap | Production | Hot-path code digest in Doer system prompt every call |
| Graphify | Production | Nightly cron full code map → Neo4j mirror |
| 5-layer Neo4j memory | Production | L0/L2/L3/L4/L5 in Neo4j; L1 = ADK Session in-mem |

## Per-agent rules

Locked in `aiforge_core/agents.yaml`. Five roles: `architect`, `planner`, `doer`, `feedback`, `learner`. Each declares model, backend, tools, `max_turns`, memory scope, termination contract.

Three-layer enforcement: ADK structural filter, GA handler reject, harness pre-flight assert. Loader/validator at `aiforge_core/agents.py`.

## Memory layout

| Layer | Role | Storage |
|---|---|---|
| L0 META | meta-procedures | NUC Neo4j `:MetaSop` |
| L1 working | per-session | ADK Session (in-mem) |
| L2 facts | global verified | NUC Neo4j `:Fact` + vector + fulltext |
| L3 SOPs | task-specific | NUC Neo4j `:Sop` |
| L4 sessions | every turn auto-remember | NUC Neo4j `:Session` + `:Turn` |
| L5 code | AST + Aider + Graphify | NUC Neo4j `:File` + `:Symbol` (+SQLite hot cache) |

Postgres holds tickets + ADK sessions (unchanged from v4).

## Quick start

Submit a ticket:

```bash
curl -X POST http://10.10.10.2:8799/api/tickets \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Add log to MyController.save",
    "body": "<acceptance criteria>",
    "assignee_role": "developer",
    "project": "PosClientBackend"
  }'
```

Watch a ticket:

```bash
curl -s http://10.10.10.2:8799/api/tickets/<id>
```

Pull facts learned (Cypher at `bolt://10.10.10.2:7687`):

```cypher
MATCH (f:Fact) WHERE f.ticket_id = $id RETURN f ORDER BY f.created_at DESC
```

## Repo layout

```
aiforge_core/
  agents.yaml                  per-agent contracts (source of truth)
  agents.py                    loader + validator
  doer/
    agent.py                   smolagents path
    ga_runner.py               GenericAgent text-protocol path (used in v5)
    orchestrator_bridge.py     dispatches to either backend
    scope_guard.py
    acceptance_gate.py
  planner/
    agent.py                   smolagents CodeAgent
  index/
    aider_map.py               Aider RepoMap wrapper
    graphify_loader.py         NetworkX → Neo4j mirror
    treesitter_ingest.py       direct AST → Neo4j
  memory/
    schema.py                  Neo4j constraints + vector + fulltext indexes
    cypher_templates.py        lookup_symbol, find_callers, find_definition
  eval/
    rule_checker.py            harness rule enforcement per agents.yaml
  runtime/
    api.py                     FastAPI :8799 (ticket CRUD + trace)
    agent_config.py            role → model + base_url
    graph_runner.py            ticket-claim daemon
docs/
  architecture.md              v5 topology + per-agent rules
  stack.md                     v4 → v5 stack changes
  agent-rules.md               per-agent rules + 3-layer enforcement
  v5-migration-plan.md         11 phases, risk register
evals/
  fixtures/F1..F7c.yaml        validated test fixtures (Java, Spring Boot)
  results/X2/                  GA standalone eval baseline (13/13 pass)
scripts/
  evals/
    run_genericagent_eval.py   X2 harness (chain mode + assertion)
    run_eval.py                AIForge ticket harness
    run_opencode_eval.py       opencode comparison (parked, mlx_lm bug)
    report.py                  aggregator
  runtime/
    com.aiforge.mlx-doer.plist     MS launchd
    com.aiforge.mlx-planner.plist  MS launchd
    graphify-nightly.sh            NUC cron
```

## Status

13/13 production-code Java runs PASS via X2 harness (F1–F7 chain v4). End-to-end via NUC API: edit-write working; `mvn compile` validation gated by NUC JDK 24 install (planned).

## Operational

| Action | Command |
|---|---|
| Restart graph-runner (NUC) | `systemctl --user restart aiforge-graph-runner.service` |
| Restart `mlx_lm` Doer (MS) | `launchctl kickstart -k gui/501/com.aiforge.mlx-doer` |
| Restart `mlx_lm` Planner (MS) | `launchctl kickstart -k gui/501/com.aiforge.mlx-planner` |
| Watch graph-runner logs | `tail -f /home/mani/.aiforge/logs/graph-runner.err` |
| Submit ticket | `POST http://10.10.10.2:8799/api/tickets` |
| Pull memory | Cypher at `bolt://10.10.10.2:7687` |
| Run eval (chain) | `.venv/bin/python scripts/evals/run_genericagent_eval.py --chain F7a,F7b,F7c` |

## Why v5 over v4

| Area | v4 | v5 |
|---|---|---|
| Orchestration | LangGraph + smolagents + LM Studio app | ADK (planned) + GenericAgent (Doer) + `mlx_lm` direct |
| Memory | pgvector + filesystem (split) | Unified in Neo4j |
| Code context | none | Aider RepoMap hot-path digest |
| Host roles | mixed across hosts | MS = models only; NUC = everything else |

## Docs

| Read | For |
|---|---|
| [`docs/architecture.md`](./docs/architecture.md) | v5 topology + per-agent rules |
| [`docs/stack.md`](./docs/stack.md) | v4 → v5 stack changes |
| [`docs/agent-rules.md`](./docs/agent-rules.md) | Per-agent rules + 3-layer enforcement |
| [`docs/v5-migration-plan.md`](./docs/v5-migration-plan.md) | 11 phases, risk register |
| [`docs/runbook.md`](./docs/runbook.md) | Ops runbook |

## License

MIT
