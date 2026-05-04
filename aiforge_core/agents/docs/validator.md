# Validator

**Role**: post-condition gate on the Doer outcome. Heuristic, **no
LLM**. Decides whether the diff is safe to surface to the Architect.

## What it does

Examines `doer_outcome` only:

| condition | decision |
|---|---|
| Doer marked `skipped: True` (no write step / target unreadable) | `skip` |
| `doer_outcome.problems` contains F-001 evidence | `block` (`no_hallucinated_imports`) |
| empty udiff | `skip` |
| no detector hits + non-empty udiff | `approve` |

In multi-step Doer mode the orchestrator calls Validator per step and
then `_aggregate_validation` rolls up: `block` if any step blocked,
`skip` if all skipped, otherwise `approve`.

## Inputs

- `doer_outcome`

## Outputs

- `validation.decision ∈ {approve, block, skip}`
- `validation.reason`
- `validation.checks` (key/bool dict)
- `validation.step_decisions[]` (multi-step mode)

## Auto-learn

Validator stays heuristic — its decisions are deterministic and feed
the Learner's success/failure roll-up. Detector hits it surfaces
become rows in `aiforge_agents_failures` so future tickets see them
in their `# Mistakes` block.

## Config

LLM-free; nothing to tune.
