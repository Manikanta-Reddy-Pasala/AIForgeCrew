# Verifier

**Role**: critic on the final plan. Decides whether the plan actually
addresses the Understanding's problem before any code is written.

## What it does

Single LLM call (json_object) returning
`{verdict ∈ {pass, repair, reject}, issues:[{step_id, kind, message}]}`.

Strips `understanding.context_md` from input — prior runs blew
2048 max_tokens when context was huge. Now strict text-only review.

## Inputs

- `understanding` (without `context_md`)
- `plan`

## Outputs

- `verifier.verdict`
- `verifier.issues[]`

`repair` is the safe default if the LLM returns invalid JSON.

## Auto-learn

Verifier itself does not consume `failures_hint`. Its decision feeds
the Learner's `outcome` calculation and is logged in
`aiforge_agents_audit` per ticket. Verifier `reject` reasons recur
chronically when Planner keeps emitting the same flawed shape, so
the Learner-derived skill ranking ends up reflecting that.

## Config

| key | default |
|---|---|
| `model` | `Qwen3-Coder-Next-MLX-4bit` |
| `temperature` | 0.0 |
| `max_tokens` | 4000 |
