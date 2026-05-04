# Understander

**Role**: read the ticket, produce a structured understanding the rest
of the pipeline can ground on.

## What it does

1. Pulls a code-graph **ContextBundle** from AiForgeMemory for the
   ticket text (vector + Lucene + 1-hop graph + reranker).
2. Auto-fetches every URL in the title/body via `runtime/web_fetch.py`,
   summarises each page through the LLM, appends the result to
   `context_md` as a new "External references" section.
3. Single LLM call (json_object) returns:
   `{problem, knowns[], unknowns[], risks[], ambiguities[], context_md}`.

## Inputs

- `ctx.title`, `ctx.body`, `ctx.repo`

## Outputs

- `understanding.problem` — restated problem statement
- `understanding.knowns` / `unknowns` / `risks` / `ambiguities`
- `understanding.context_md` — markdown blob the Planner / Doer
  consume for grounded references

## Auto-learn

The Understander itself does not write to the failure-memory store, but
it *reads* the current AiForgeMemory state (which the Learner enriches
across runs) and pulls any external URL the user pasted into the body.
That means:

- **Drop a vendor doc URL into the ticket** → next stages see a
  bullet-summary of the doc.
- **AiForgeMemory has been updated by prior runs** → the bundle
  reflects fresh repo state automatically.

## Config

| key | default |
|---|---|
| `model` | `Qwen3-Coder-Next-MLX-4bit` |
| `temperature` | 0.3 |
| `max_tokens` | 8000 |
