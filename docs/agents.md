# Agents & ADK Orchestrator

Five roles. ADK `SequentialAgent` runs them in order. `LoopAgent` retries
the doer/feedback inner loop up to 4 times.

## The five roles

```
                   Architect (external Claude Code)
                            │
                            │  writes ticket via API
                            ▼
                  ┌─────────────────────┐
                  │      Planner        │  direct LiteLLM, 1 shot
                  │   ↓ plan.md         │
                  └─────────────────────┘
                            │
                            ▼
              ┌─── LoopAgent (max 4 iter) ───┐
              │                              │
              │     Doer  ──→  Feedback      │  doer = GA loop, fb = LiteLLM 1 shot
              │      (GA)         (LiteLLM)  │
              │                              │
              └──────────────────────────────┘
                            │
                            ▼
                   ┌─────────────────────┐
                   │      Learner        │  direct LiteLLM, 1 shot, writes :Fact
                   └─────────────────────┘
                            │
                            ▼
                   commit → push → PR
```

## Per-agent contracts

Source of truth: `aiforge_core/agents.yaml`. Loader: `aiforge_core/agents.py`.

| Role | Backend | Model | Max turns | Max wall |
|---|---|---|---|---|
| Architect | external | Claude Code (laptop) | n/a | n/a |
| Planner | direct LiteLLM | Qwen3.6-27B (`:1235`) | 1 (single completion) | 1200 s |
| Doer | GenericAgent text-protocol loop | Qwen3-Coder-Next (`:1234`) | 30 | 700 s |
| Feedback | direct LiteLLM | Qwen3-Coder-Next | 1 | 60 s |
| Learner | direct LiteLLM + plugin write | Qwen3-Coder-Next | 1 | 60 s |

## What each role does

### Architect (external)
Submits a ticket. Doesn't run inside ADK.

```bash
curl -X POST http://10.10.10.2:8799/api/tickets -d '{"title":...,"body":..."}'
```

### Planner
- **Input**: ticket title + body + auto-injected L2 facts + L3 SOPs.
- **Action**: emits a markdown plan with `[ ]` checkboxes to
  `<worktree>/.aiforge/plan.md`.
- **No tools.** Single LiteLLM call. Model has 1500-token output budget.
- **Output schema**:
  ```
  ## Goal
  ## Files
  ## Steps
  - [ ] step 1
  - [ ] step 2
  ## Acceptance criteria
  ```

### Doer
- **Input**: ticket body + plan.md + worktree path + auto-injected L2
  facts + Aider RepoMap digest + L5 graph_lookup on demand.
- **Action**: enters GA plan mode against `plan.md`; works through the
  checkbox list, marking `[x]` as steps complete. Plan mode auto-exits
  when all boxes drained.
- **Tools (filtered subset of GA's 9)**: `file_read`, `file_write`,
  `file_patch`, `code_run`, `ask_explorer`. ScopeGuard blocks writes
  outside ticket allowlist.
- **Forbidden**: `ask_user`, `start_long_term_update`,
  `web_scan`, `web_execute_js`. Three-layer enforcement (ADK schema
  filter, GA `tool_before_callback` reject, harness pre-flight assert).
- **Stop condition**: compile green AND every acceptance bullet
  reflected in the diff, OR 2 consecutive compile failures.

### Feedback
- **Input**: ticket body + diff + compile result + plan.md.
- **Action**: emits a verdict: `pass`, `fail`, or `scope_violation`.
- **No tools.** Single LiteLLM call. JSON output.

### Learner
- **Triggered**: only when feedback says `pass`.
- **Action**: distills "what worked here" into 1–3 sentences. ADK
  plugin writes the result as a `:Fact` node in Neo4j with vector
  embedding.
- **No tools.** Single LiteLLM call.

## ADK orchestrator wiring

The pipeline is one `SequentialAgent` containing four agents (planner +
loop + learner). The loop wraps doer + feedback for retry.

```python
# aiforge_core/runtime/adk_workflow.py (sketch)
planner   = AiForgePlannerAgent(name="planner")
doer_loop = LoopAgent(
    name="doer_chain",
    sub_agents=[
        AiForgeDoerAgent(name="doer"),
        AiForgeFeedbackAgent(name="feedback"),
    ],
    max_iterations=4,
)
learner   = AiForgeLearnerAgent(name="learner")

workflow = SequentialAgent(
    name="aiforge",
    sub_agents=[planner, doer_loop, learner],
)

runner = Runner(
    agent=workflow,
    session_service=DatabaseSessionService(
        db_url="postgresql+asyncpg://...",  # NUC Postgres
    ),
    plugins=[Neo4jMirrorPlugin()],          # auto-write :Turn per Event
)
```

## How agents talk to each other

ADK `Session.state` is the shared memory. Each agent writes a few keys
on completion:

```
S_PLAN_DONE         True after planner finishes
S_LAST_DOER_SUMMARY one-line summary doer emits at end
S_FAIL_COUNT        feedback.fail counter (drives LoopAgent escalation)
S_COMPILE_FAIL_COUNT consecutive compile failures
```

The next agent reads from `ctx.session.state`. No file passing, no
out-of-band channels.

## Backend dispatch

`aiforge_core/doer/orchestrator_bridge.py` reads `AIFORGE_DOER_BACKEND`
env (or `agents.yaml` declaration) and routes to either:

- `run_doer_via_ga` — GenericAgent text-protocol loop (production)
- `run_smolagents_doer` — legacy smolagents path (fallback)

Same pattern in `aiforge_core/runtime/adk_workflow.py:AiForgePlannerAgent`
for the planner role.

## Auto-remember (per turn)

`Neo4jMirrorPlugin.on_event_callback` fires for every ADK Event before
it's persisted to the session service. Writes a `:Turn` node into Neo4j
linked to the active `:Session` and `:Ticket`. Cannot be opted out.

## Per-agent rules — three-layer enforcement

| Layer | Where | What |
|---|---|---|
| 1. Structural filter | ADK | Per-agent `tools=[...]` list — model never sees forbidden tools |
| 2. Runtime reject | GA handler `tool_before_callback` | Catches the model if it hallucinates a forbidden tool by name |
| 3. Harness assert | `aiforge_core/eval/rule_checker.py` | Post-run scan of trace events; fails the run if a forbidden tool name appears |

All three layers consume the same `agents.yaml`. Drift impossible.

## Live agent control

Two API endpoints can steer a running agent without restart:

```bash
# Halt a runaway agent
curl -X POST .../tickets/<id>/intervene \
     -d '{"kind":"stop"}'

# Inject a hint into the next user prompt
curl -X POST .../tickets/<id>/intervene \
     -d '{"kind":"intervene","body":"focus on src/main, skip tests"}'

# Update working memory key_info
curl -X POST .../tickets/<id>/intervene \
     -d '{"kind":"keyinfo","body":"PaymentInDao has setReceiptNumber not setPaymentNumber"}'

# Lower temperature for next agent run
curl -X POST .../runtime/session_param \
     -d '{"role":"doer","key":"temperature","value":0.05}'
```

The first three write GA's `_stop` / `_intervene` / `_keyinfo` files
into the live agent's task_dir; GA's `turn_end_callback` polls them.

## Pointers

- Workflow definition: `aiforge_core/runtime/adk_workflow.py`
- Daemon entry point: `aiforge_core/runtime/adk_runner.py`
- Per-agent contracts: `aiforge_core/agents.yaml`
- Doer GA adapter: `aiforge_core/doer/ga_runner.py` + `ga_compat.py`
- Planner adapter: `aiforge_core/planner/ga_runner.py`
- Backend dispatch: `aiforge_core/doer/orchestrator_bridge.py`
- API endpoints: `aiforge_core/runtime/api.py`
