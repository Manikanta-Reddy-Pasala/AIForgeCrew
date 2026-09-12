# aiforge_core.agents — v6 pipeline archetypes

One module per archetype, each exposing `build(model_factory)` and returning an
ADK node. `runtime/pipeline.py:build_pipeline()` is the only place that knows
what order they run in — adding a role means dropping a module here and slotting
it into that graph.

Source of truth for tools, max_turns, memory scope and termination contracts is
[`agents.yaml`](agents.yaml); sampling defaults are in
[`agents.defaults.yaml`](agents.defaults.yaml). The `identity.model` fields in
`agents.yaml` are informational — routing is resolved at runtime (see below).

## Graph

ADK 2.x deprecated `SequentialAgent` / `ParallelAgent` / `LoopAgent`, so the
pipeline is a `google.adk.workflow.Workflow`: explicit nodes wired by `Edge`s,
with conditional routing where a node emits `ctx.route` and the matching edge
fires.

```
START → triage → triage_gate ──trivial─────────────────────► doer
                             └──full──► enhancer
    enhancer ─┬► researcher ──┐
              ├► ctx_repomap ─┤ (parallel) → context_join → merge_context
              └► ctx_conv? ───┘  (conventions skipped when repo rules exist)
                                                                 │
                                                                 ▼
    planner → verifier (1 call: correctness+scope+risk) → verifier_gate
             ──pass──► doer    ──replan──► planner
    doer → refiner → feedback → loop_gate ──loop──► doer
                                         └──exit──► validator
    validator → validator_gate ──replan──► planner   (once)
                               └──done────► learner
```

Gates replace the old composite agents: `loop_gate` (iteration count + LOC
plateau + feedback verdict) replaces `LoopAgent`, `triage_gate` takes the
trivial fast path, and `verifier_gate` / `validator_gate` own the replan edges.
Gate factories live in `runtime/graph_pipeline`; the parallel context fan-out is
`runtime/parallel_stages`.

## Archetypes

| Module | Mode | Role |
|---|---|---|
| `triage` | single-turn | Classifies ticket complexity; seeds the fast-path gate |
| `enhancer` | chat | Rewrites the raw ticket body into a structured brief |
| `researcher` | chat | Read-only context gather (skippable per ticket) |
| `ctx_repomap` · `ctx_conventions` | chat | Parallel context branches beside the researcher |
| `planner` | chat | Plan + child subtickets + scope allowlist |
| `verifier` | single-turn | One call judging correctness, scope and risk together |
| `doer` | chat | Edits files in the allowlist; runs tools under ScopeGuard |
| `refiner` | chat | Behaviour-neutral diff polish before feedback |
| `feedback` | chat | Post-execution judge; drives `loop_gate` |
| `gap_eval` | single-turn | Research-gap critic; built only when the researcher ran |
| `validator` | single-turn | Post-loop check; may force one replan |
| `learner` | chat | Writes memory rows on success |
| `live_verifier` | — | Recipe-driven live check; the recipe is baked into the prompt, not the graph |

`architect.py` and `ctx_memory.py` are defined but not wired into the v6 graph
(`ctx_memory` was dropped from the context fan-out in 2026-06). Likewise
`verify_correctness.py` / `verify_scope.py` / `verify_risk.py` — the three
parallel verify branches were collapsed into the single `verifier` call, which
judged the same plan for a third of the tokens.

## Routing

Per-role provider and model come from `~/.aiforge/agent_config.json` via
`aiforge_core.config.agent_config.resolve_litellm`, overridable per role with
`AIFORGE_<ROLE>_*` env vars, and editable from the Settings UI through
`/api/agents/v2/*`. Every `LlmAgent` is wrapped in an `EscalatingLlm`, so a
local mlx-lm primary fails over to the operator's cloud chain without the graph
knowing about it.

## ADK version

Pinned `google-adk>=2.1.0,<2.2` in `pyproject.toml`. The upper bound is
deliberate: 2.3.x added a Workflow validation that rejects this graph (chat-mode
agents following a gate) and breaks `build_pipeline`.

## Files

| Path | Role |
|---|---|
| `agents.yaml` | Per-agent contract — tools, max_turns, memory scope, termination |
| `agents.defaults.yaml` | Per-archetype sampling defaults |
| `loader.py` | Parses and validates `agents.yaml` into `AgentContract`s |
| `_base.py` | `build_llm_agent()` + per-role contract lookup and wall-clock budget |
| `<role>.py` | One module per archetype, each a `build(model_factory)` factory |
| `../runtime/pipeline.py` | Wires the Workflow graph |
| `../runtime/graph_pipeline/` | Gate factories (triage, verifier, loop, validator) |
| `../runtime/parallel_stages.py` | Context fan-out, join and merge nodes |
| `../runtime/prompts/` · `prompts_extended/` | Prompt text |
| `../recipes/` | Per-project deploy + live-verify recipes |
