# Grounder

**Role**: verify every plan step's target actually resolves in the
AiForgeMemory File_v2 graph. Rule-based, **no LLM call**.

## What it does

For each step:

| action | check |
|---|---|
| `read \| edit \| test \| run` | path exists in graph (exact OR ends-with-basename match) **OR** was created by an earlier step in the same plan (order-aware) |
| `create` | reject if file already exists (suggest `edit`); otherwise walk parent dirs until any ancestor has at least one indexed file (allows fresh feature/test packages) |
| `search`, no target | always pass |

Top-level paths (`README.md` etc.) auto-pass.

## Inputs

- `plan.steps[]`
- `repo`

## Outputs

- `grounding.resolved` — bool
- `grounding.unresolved_refs[]` — `[{step_id, target, action, reason}, …]`

Reasons emitted: `file_missing`, `parent_dir_missing`,
`file_already_exists_use_edit`.

## Auto-learn

Grounder is purely deterministic — it does not query Postgres failure
memory. But its rejections feed the Planner's REPLAN loop directly,
and every rejection becomes part of the Learner's failure record at
ticket-end (so future Planners see a "DO NOT use missing path X again"
lesson).

## Config

LLM-free; only the Neo4j connection envs apply
(`AIFORGE_NEO4J_URI` / `_USER` / `_PASSWORD`).
