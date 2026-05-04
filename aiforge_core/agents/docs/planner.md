# Planner

**Role**: turn an Understanding into a granular plan of small,
single-file steps.

## What it does

Single LLM call (json_object) producing
`{steps:[{id, action, target, inputs, expected, depends_on}], expected_token_budget}`.

Action set: `read | edit | test | run | create`.

Hard constraints in the system prompt:

- **Granularity**: each `create` step produces ONE file. Splitting a
  feature into 5 single-file creates beats 2 multi-file creates.
- **Strict allowlist**: every `read|edit|test|run` target must be a
  path from the seeded `allowed_files` list. `create` paths must be
  under an allowed package directory.
- **Step cap**: max 12.

## Inputs

- `understanding` (compacted: head 4000 + tail 2000 of `context_md`)
- `allowed_files[]` — top-80 from AiForgeMemory hybrid retrieval
- `skills_hint[]` — winning recipes for this `task_class`
- `failures_hint[]` — chronic mistakes for this `task_class`
- `previous_plan` + `unresolved_refs[]` (REPLAN feedback)

## Outputs

- `plan.steps[]`
- `plan.expected_token_budget`
- `plan.depth_violation` if step count > 12

## Auto-learn

- **Skills block**: top 3 historical winning recipes (`✓N/✗M`).
- **Failures block**: top 5 chronic mistakes with `mode`, `evidence`,
  `lesson` lines: *"DO NOT REPEAT"*. Sourced from
  `aiforge_agents_failures` (Learner appends after every run).
- **REPLAN loop**: if Grounder rejects targets the unresolved refs
  flow back to Planner with explicit "previous attempt was BLOCKED"
  block + the bad targets.
- **Empty-plan REPLAN**: if Planner returns no steps, orchestrator
  forces another attempt with `planner_returned_no_steps` reason.

## Config

| key | default |
|---|---|
| `model` | `Qwen3-Coder-Next-MLX-4bit` |
| `temperature` | 0.3 |
| `max_tokens` | 24000 |

Post-Planner filter (`orchestrator._filter_plan_targets`) drops any
step whose target is not in `allowed_files` (prevents naming-style
hallucinations from reaching Grounder).
