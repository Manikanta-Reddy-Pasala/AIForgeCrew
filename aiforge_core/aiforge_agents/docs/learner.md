# Learner

**Role**: distill every ticket into reusable signals — episodic
records, procedural patterns, **skills**, and **failures**. The
single source of cross-ticket auto-correction.

## What it does

After every ticket the Learner reads the full pipeline output and
writes 4 kinds of rows to Postgres:

| Table | What |
|---|---|
| `aiforge_agents_episodic` | one row per run: outcome, summary, artifacts |
| `aiforge_agents_procedural` | tool sequence per (agent_role, task_class) with success/failure counters |
| `aiforge_agents_skills` | named recipes promoted on success, also tracked on failure for net-success ranking |
| `aiforge_agents_failures` | every detector hit + apply error becomes a `(repo, task_class, mode, evidence, lesson)` row with seen-count |

`task_class` is derived from `doer_outcome.target` — feature-dir name
when path has ≥2 segments, otherwise the basename. Top-level files
(README.md) auto-pass.

## Inputs

- `ticket_id`, `repo`, `plan`, `verifier_verdict`, `grounding`,
  `doer_outcome`, `validation`, `review`

## Outputs

- `learning.outcome ∈ {success, blocked, rejected}`
- `learning.task_class`
- `learning.tool_sequence[]`
- `learning.summary`

## Success criteria

Authoritative gate (Architect approval is final):

```python
success = (
    grounding.resolved
    and validation.decision == "approve"
    and review.decision == "approve"
)
```

Verifier `pass` is **not** required — too rare on local-LLM stack
where the Verifier often falls back to `repair` on JSON truncation.

## Auto-learn — what gets recorded

For every detector problem on the Doer outcome:

```text
mode: F-001
evidence: com.bogus.LedgerCategoryDto
lesson: "Earlier ticket emitted import `com.bogus.LedgerCategoryDto`
         that did not exist. Restrict imports to stdlib +
         plan_create_fqns + existing graph classes; never invent
         sub-packages."
```

For every apply error:

```text
mode: apply_check
evidence: "error: corrupt patch at line 30"
lesson: "Doer udiff failed to apply. Match the exact udiff format:
         `--- /dev/null` for new files, accurate @@ hunk counts
         (recount-friendly), no truncation."
```

Skill rows on success include a body with the winning tool sequence,
plan size, and target — agents recall these via `top_skills_for`.

## How agents recall

The orchestrator, once per ticket:

```python
skills_hint   = learner.top_skills_for(repo=repo,
                                       task_class=guess(title, body))
failures_hint = learner.top_failures_for(repo=repo,
                                         task_class=guess(title, body))
```

Both lists flow into Planner / Doer / Tester / Architect prompts via
`runtime/prompt_helpers.render_failures_block` — a single rendering
function so headers stay contextual and consistent.

## Config

| key | default |
|---|---|
| `model` | (heuristic only — no LLM call by default) |
| `max_tokens` | 4000 |

To enable LLM-distilled lessons, set `model` to a real path; the
heuristic templates stay as a fallback.
