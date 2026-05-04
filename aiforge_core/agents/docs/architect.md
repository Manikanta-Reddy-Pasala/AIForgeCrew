# Architect

**Role**: read-only review of the applied diff vs the original
Understanding + Plan. Drafts the MR title + body. Optionally creates
the actual GitHub PR.

## What it does

1. If `validation.decision != "approve"` — short-circuit with
   `request_changes` + `validation blocked: <reason>` comment.
   No LLM call.
2. Else single LLM call (json_object) returning
   `{decision ∈ {approve, request_changes, reject}, comments[],
   mr_title, mr_body}`.
3. If `ctx.open_mr=True` AND `doer_outcome.applied=True` AND
   `decision != "reject"` → push branch + run `gh pr create`.
   - Auto-detects base branch via `git symbolic-ref`.
   - `--draft` flag when `decision == "request_changes"` so reviewers
     see a clear "needs work before merge" signal.
   - Returns the PR URL into `review.mr_url`.

## Inputs

- `understanding` (with `context_md` stripped)
- `plan`, `doer_outcome` (with udiff compacted to head 12000 + tail 4000)
- `validation`
- `failures_hint`
- `repo_path`, `open_mr` (caller toggles via `AIFORGE_AGENTS_OPEN_MR=1`)

## Outputs

- `review.decision`
- `review.comments[]`
- `review.mr_title` (≤70 chars)
- `review.mr_body` (markdown w/ `## Summary` / `## Changes` / `## Tests`)
- `review.mr_url` (populated only when `gh pr create` ran)

## Auto-learn

- Receives `failures_hint` with header *"Mistakes from prior reviews —
  flag if seen here"* so the Architect catches the same issues that
  bit earlier tickets.
- After `request_changes` the orchestrator triggers a Doer→Architect
  retry: the Doer gets the architect comments as feedback and
  produces a revised diff; Architect re-reviews.

## Config

| key | default |
|---|---|
| `model` | `Qwen3-Coder-Next-MLX-4bit` |
| `temperature` | 0.0 |
| `max_tokens` | 12000 |
