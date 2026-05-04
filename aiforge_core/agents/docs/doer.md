# Doer

**Role**: turn one plan step into a unified diff, then apply it to a
ticket branch and commit.

## What it does

The orchestrator iterates `write_steps` (action=create|edit) and
invokes Doer ONE TIME PER STEP. Each step:

1. Read target file (for `edit`) or set empty file_text (for `create`).
2. Build a constrained prompt with:
   - **Failures block** — chronic mistakes for this `task_class`.
   - **CRITIC feedback** — previous-attempt udiff + detector hits +
     architect comments (when retrying).
   - **Sibling block** — exact FQNs being created elsewhere in the
     same plan, plus a STRICT IMPORT RULES paragraph forbidding
     sub-package guessing.
   - Compacted target file body (head 12000 + tail 4000).
3. LLM emits a fenced ```diff block (truncation detected → mark
   `apply_error="truncated_output"` and skip apply).
4. Run detectors:
   - **HallucinatedImportDetector** (F-001) — extended stdlib
     allowlist (Spring/Lombok/Swagger/Jackson/JUnit/Mockito/Mongo/
     NATS/Reactor/Slf4j); plan-create-FQN sibling whitelist;
     graph fallback. Filters Java syntax tokens (`static`/`type`).
   - **DiffContextHashDetector** (F-003) — hunk context lines must
     hash-match the actual file (edit only).
5. Save proposal patch to `<repo>/.aiforge/proposals/<ticket>.patch`.
6. **Apply** (when `ctx.apply=True` AND no detector hits):
   - Detect remote default branch via
     `git symbolic-ref refs/remotes/origin/HEAD`.
   - If `aiforge/<ticket>` already exists locally → `git checkout`
     (subsequent multi-step calls stack on the same branch).
   - Else → `git checkout -B aiforge/<ticket> <base>` (fresh fork).
   - `git apply --check --recount --ignore-whitespace --whitespace=nowarn`
     then real apply, then commit with `:(exclude).aiforge`,
     `:(exclude).aiforge-worktrees`, `:(exclude)graphify-out`,
     `:(exclude).idea`, `:(exclude).vscode`.
   - Refuses dirty worktree (filtered ignore-set).

## Inputs

- `target_step` (one step dict) — set by orchestrator
- `plan`, `repo`, `repo_path`, `ticket_id`, `apply`
- `previous_udiff`, `detector_problems`, `architect_comments` (CRITIC)
- `failures_hint`

## Outputs

- `doer_outcome.udiff`
- `doer_outcome.problems[]`
- `doer_outcome.applied` / `applied_branch` / `apply_error`
- `doer_outcome.artifact_path`

## Auto-learn

- Reads the same `failures_hint` as Planner — `# Mistakes from prior
  similar tickets — DO NOT REPEAT` is rendered into every Doer prompt
  via `runtime/prompt_helpers.render_failures_block`.
- Within a single ticket: per-step CRITIC retry loop (2 attempts) —
  validator block on attempt 1 returns the udiff + detector hits as
  `previous_udiff` / `detector_problems` for attempt 2.
- Architect→Doer retry: when validator approves but architect
  requests changes, one extra Doer pass receives architect comments.

## Config

| key | default |
|---|---|
| `model` | `Qwen3-Coder-Next-MLX-4bit` |
| `temperature` | 0.2 |
| `max_tokens` | 24000 |

Set `AIFORGE_AGENTS_APPLY=1` to enable the git-apply path.
