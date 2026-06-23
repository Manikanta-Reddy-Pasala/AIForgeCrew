# aiforge_core.agents — v6 production pipeline

**Nine archetypes** (six core + three extended), executed by
`runtime.adk_runner` as an ADK `SequentialAgent`. The extended trio
(`triage`, `researcher`, `refiner`) was added 2026-05-07 to lift
ticket-completion rate by gathering context up-front, polishing diffs
before review, and routing each ticket to the right model tier.

```
architect (external)
        │
        ▼
   triage ──► planner ──► verifier ──► researcher ──► LoopAgent
                                                     ╔═══════════════════╗
                                                     ║ doer ─► refiner ─►║──► learner
                                                     ║      feedback     ║
                                                     ╚═══════════════════╝
                                                     loop until
                                                     feedback.verdict ∈
                                                     {pass, fail, scope_violation}
                                                                │
   verifier reject  ◄────────── re-plan (cap 3) ────────────────┘
```

`triage` runs in the orchestration layer (not inside the ADK
`SequentialAgent`) so its complexity verdict can seed
`runtime.model_router` *before* downstream agents pick their providers.

| # | Archetype | Runtime | Role |
|---|-----------|---------|------|
| 1 | **architect**  | external operator session (human-driven) | Writes parent ticket; never edits code |
| 2 | **triage** *(new)* | ADK direct LiteLLM (single completion) | Classifies ticket complexity (`trivial`/`moderate`/`hard`); drives `model_router` tier selection |
| 3 | **planner**    | ADK + GenericAgent text-protocol       | Reads parent ticket → emits plan + child subtickets + scope allowlist |
| 4 | **verifier**   | ADK direct LiteLLM (single completion) | Pre-execution plan critic. Reject → re-plan with issues folded in. Layered with `verifier_strict` for deterministic structural checks |
| 5 | **researcher** *(new)* | ADK + GenericAgent text-protocol | Read-only context gatherer. Calls `graphify_lookup` + `memory_lookup` + `file_read` to assemble a `research_brief.md` per subticket so the Doer skips exploration |
| 6 | **doer**       | ADK + GenericAgent text-protocol       | Edits files inside the subticket allowlist; runs compile + tests |
| 7 | **refiner** *(new)* | ADK direct LiteLLM (single completion) | Behaviour-neutral diff polish before Feedback. Tool-less. Set `refiner_skipped=true` when the diff is already clean |
| 8 | **feedback**   | ADK direct LiteLLM (single completion) | Post-execution judge. Emits JSON `{verdict, rationale}` |
| 9 | **learner**    | ADK direct LiteLLM (single completion) | Runs only on `feedback.verdict=pass`. Writes `:Fact` rows to memory |

Source of truth for tools, max_turns, memory scope, and termination
contract: [`agents.yaml`](agents.yaml).

## Pipeline orchestration

The Sequential pipeline is built in
`aiforge_core/runtime/pipeline.py:build_pipeline()`. Shape:

```
SequentialAgent[
    planner,
    verifier,
    researcher,
    LoopAgent[doer, refiner, feedback]   # max_iterations = 3
    learner,
]
```

`triage` is invoked upstream by `adk_runner._process_one_ticket`
*before* `build_pipeline()` is called. Its output is stored in session
state and read by:

- `runtime.model_router.pick(role, complexity)` — tier selection
- `runtime.verifier_strict.apply(plan, base_verdict)` — caps depend on
  expected file count

## Tool surface

| Tool | Read/Write | Used by |
|------|------------|---------|
| `file_read`        | read  | researcher, doer (refiner sees diffs only — no fs access) |
| `file_write`       | write | doer |
| `file_patch`       | write | doer |
| `list_dir`         | read  | researcher, doer |
| `run_shell`        | exec  | doer (`code_run` in agents.yaml — wrapped by ScopeGuard) |
| `memory_lookup`    | read  | architect, planner, researcher, doer |
| `graphify_lookup` *(new)* | read | architect, planner, researcher, doer |

`graphify_lookup` exposes the typed graphify graph (`calls` / `uses` /
`contains` / `inherits` / `method` / `imports_from` / `rationale_for`)
through a queryable interface — see
`aiforge_core/runtime/graphify_lookup_tool.py`. The `rationale_for`
edges are graphify-only (LLM-extracted) and are not derivable from
tree-sitter / AST ingest alone.

The three single-turn judges (`triage`, `verifier`, `feedback`,
`learner`, `refiner`) are tool-less by design — their termination
contracts demand a single JSON verdict, no tool calls.

## Provider routing

Per-archetype provider + model is configurable at runtime via
`~/.aiforge/agent_config.json` (see `aiforge_core.config.agent_config`).
Bulk presets:

```bash
aiforge-profile apply ollama_cloud   # all archetypes → qwen3-coder:480b
aiforge-profile apply local          # all archetypes → LM Studio MLX

aiforge-profile set planner    ollama_cloud qwen3-coder:480b
aiforge-profile set doer       ollama_cloud qwen3-coder:480b
```

Settings UI exposes the same surface at `/api/agents/v2/*`.

### Tier-aware routing (`runtime.model_router`)

When triage emits `complexity` ∈ `{trivial, moderate, hard}` the router
picks a per-role tier list:

| Role       | Tier 0 (cheapest) | Tier 1 (default) | Tier 2 (escalate) |
|------------|-------------------|------------------|-------------------|
| doer       | `Devstral-Small-2-24B-Instruct-2512-4bit` | `Qwen3-Coder-Next-MLX-4bit` | `qwen3-coder:480b` (ollama_cloud) |
| researcher | `Qwen3.6-27B-MLX-4bit` | `Qwen3.6-35B-A3B-MoE` | — |
| refiner    | `Qwen3.6-27B-MLX-4bit` | — | — |
| triage     | `Qwen3.6-27B-MLX-4bit` | — | — |

`model_router.next_doer_model_after_fail()` escalates the Doer one tier
on the first compile-fail instead of waiting for two consecutive
failures (the original termination contract). Operator overrides via
`AIFORGE_<ROLE>_MODEL` env or `agent_config.json` always win.

## Parallel doer dispatch (`runtime.parallel_doer`)

When the planner emits multiple leaf subtickets with non-overlapping
`scope_allowlist_globs`, `parallel_doer.batch()` groups them into
sequential batches that are safe to run concurrently. Two scopes
"conflict" when their literal prefixes share a directory — different
files in the same dir (e.g. `foo.py` and `bar.py`) are NOT a conflict.

Default `max_parallel=3` matches the LM Studio `parallel` slot count
shipped with the runtime config.

## ADK version

`google-adk==2.0.0b1` (pinned `>=2.0.0b1` in `pyproject.toml`). Migration
from 1.31.1 happened 2026-05-07 — see
`docs/superpowers/plans/2026-05-04-adk-2.0b1-migration.md` for the
original plan. The 1.x → 2.0.0b1 import surface is API-compatible, so
no agent code rewrite was needed; we just bumped the pin and reset the
Postgres `adk_sessions` schema.

ADK 2.0.0b1 features the runtime relies on:

| Feature | Used by | Where |
|---------|---------|-------|
| `BaseAgent` / `LlmAgent` | every archetype | `agents/_base.py:build_llm_agent` |
| `LoopAgent` | doer/refiner/feedback iteration | `runtime/pipeline.py` |
| `SequentialAgent` | top-level pipeline | `runtime/pipeline.py` |
| `FunctionTool` | doer + researcher tool surface | `runtime/doer_tools.py` |
| `Event` / `EventActions` (state_delta) | per-turn trace events | runtime plugins |
| `BasePlugin.on_event_callback` | non-opt-out `:Turn` node emission | runtime plugin layer |
| `DatabaseSessionService` | Postgres-backed sessions | `runtime/adk_runner.py` |
| `InMemorySessionService` | unit-test fixtures | tests |
| `Runner(auto_create_session=True)` | session bootstrap | `runtime/adk_runner.py` |
| `RequestInput` (HITL) | follow-up — `ask_user` migration deferred | tracked elsewhere |

## Files

| Path | Role |
|------|------|
| `agents.yaml`            | per-agent contract (tools, max_turns, memory scope) |
| `loader.py`              | reads + validates `agents.yaml` |
| `registry.py`            | `registry.build(name)` — only consumer of archetype classes |
| `base.py`                | `BaseArchetype` — provider + model + tool plumbing |
| `architect.py` · `planner.py` · `verifier.py` · `doer.py` · `learner.py` | core archetype implementations |
| `defaults.py` · `config.py` | per-archetype default sampling params |
| `docs/`                  | per-archetype design notes |
| `../runtime/prompts.py`            | core archetype prompts |
| `../runtime/prompts_extended.py` *(new)* | triage / researcher / refiner prompts |
| `../runtime/research_brief.py` *(new)*   | parser + markdown renderer for researcher output |
| `../runtime/model_router.py` *(new)*     | complexity → tier resolution + escalation |
| `../runtime/parallel_doer.py` *(new)*    | scope-disjoint subticket batching |
| `../runtime/verifier_strict.py` *(new)*  | deterministic post-pass on Verifier verdict |

`feedback`, `triage`, `refiner` have no archetype class — they're
single-completion judges/classifiers wired directly into the pipeline,
see `runtime/pipeline.py`.
