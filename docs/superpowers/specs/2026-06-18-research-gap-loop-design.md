# Research-Gap Loop — Design

**Date:** 2026-06-18
**Status:** Approved (design); pending implementation plan
**Author:** pairing session

## Problem

The v6 ADK `Workflow` pipeline has three bounded reflect→replan loops
(plan-critique, Doer-execution, Validator second-opinion) but **no
research-completeness loop**. The context fan-out
(`researcher ‖ ctx_repomap ‖ ctx_conventions`) runs once, single-pass.
`researcher.py` deliberately stops "as soon as each subticket has ≥1
relevant_files entry … not to over-research." Nothing inspects whether
the assembled `context_brief_md` is actually *sufficient* for the
Planner. If the Researcher missed a file or topic, the Planner plans
against an incomplete brief and the gap only surfaces later (Doer loop,
Validator, or a failed PR).

This adds the one self-correction loop the pipeline lacks: an
evaluator that judges research sufficiency and re-dispatches a targeted
re-search before planning. Mirrors the "reflect → gap-analysis →
replan research" pattern from the reference article, scaled to a coding
agent.

## Approach (chosen)

LLM gap-evaluator node + bounded single re-search pass, re-firing the
**whole context fan-out** (not researcher-alone).

Rejected alternatives:
- *Planner-emitted needs* — couples Planner to the research loop; gap
  surfaces one stage too late.
- *Deterministic empty-`relevant_files` check* — free but can't detect
  "present but insufficient" research.
- *Researcher-only re-dispatch* — would stall the ADK `JoinNode`:
  `context_join` re-arms only when **all** its in-branches fire in one
  scheduler wave (documented ONE-117 constraint in `pipeline.py`
  `max_concurrency` floor=3). Re-firing one branch leaves the join
  seeing stale `COMPLETED` status on the other two and firing early with
  stale briefs.

## Architecture

New graph shape (insert between `merge_context` and `planner`):

```
enhancer → research_entry ─┬► researcher ──┐
                           ├► ctx_repomap ─┤ → context_join → merge_context
                           └► ctx_conv? ───┘                       │
                                                                   ▼
                            planner ◄──research_ok── gap_gate ◄── gap_eval
                                ▲                        │
       research_entry ◄─research_gap─────────────────────┘
       (re-fires fan-out; research_gap_brief_md injected into researcher)
```

### New components

1. **`research_entry`** — passthrough `FunctionNode` (mirrors
   `plan_promote`). Becomes the **fan-out source**. Both first pass
   (from `enhancer`) and the loop (from `gap_gate`) re-enter here, so the
   full context fan-out re-fires in one scheduler wave and `context_join`
   re-arms cleanly. No multi-source-edge ambiguity on the branch nodes.
   Body is a no-op (state passthrough); exists purely as a stable
   re-entry point.

2. **`gap_eval`** — single-turn, tool-less `LlmAgent` (local model via
   `build_litellm_model`, same tier as the verifiers). Reads: ticket +
   enhanced ticket + `context_brief_md` (+ `memory_brief_md`). Emits
   JSON to `OUTPUT_KEY = "gap_verdict"`:
   ```json
   {
     "sufficient": false,
     "missing": ["AuthService token-refresh path", "config for X"],
     "queries": ["where is token refresh handled", "X config file"]
   }
   ```
   Prompt lives in `prompts_extended.GAP_EVAL`.

3. **`gap_gate`** — `FunctionNode` in `graph_pipeline.py`.
   - `MAX_GAP_PASSES = 1` (matches `MAX_REPLANS` / `MAX_VERIFY_REPLANS`).
   - Reads `gap_verdict` (via a `_coerce_verdict`-style tolerant parse)
     and `gap_pass_count`.
   - If `sufficient == false` **and** `gap_pass_count < MAX_GAP_PASSES`:
     increment `gap_pass_count`; render `missing`/`queries` into
     `research_gap_brief_md` state key; route `ROUTE_RESEARCH_GAP`.
   - Else route `ROUTE_RESEARCH_OK` → `planner`.

### Researcher prompt change

`prompts_extended.RESEARCHER` gains an optional block:
`{research_gap_brief_md?}` — "A prior research pass was judged
incomplete. Specifically locate: …". On the first pass the key is
absent (block renders empty); on the loop pass it drives targeted
re-search. Harmless to `ctx_repomap` / `ctx_conventions` (they don't
read the key).

## Data flow

`research_gap_brief_md` (set by gap_gate) → `researcher` prompt →
fresh `research_brief_md` → `merge_context` rebuilds `context_brief_md`
→ `gap_eval` re-judges → `gap_gate` sees `gap_pass_count == 1` → forces
`ROUTE_RESEARCH_OK` → `planner`.

## Edge cases

- **`skip_researcher == True`** (greenfield routing): the gap loop is
  meaningless (nothing to re-search). `build_pipeline` omits `gap_eval`
  + `gap_gate` entirely and wires `merge_context → planner` directly
  (current behaviour). Equivalent: gate hard-routes `research_ok`.
- **`gap_eval` parse failure / unparseable JSON** → treat as
  `sufficient: true` (never block the pipeline on a critic's formatting
  slip — same convention as `parallel_stages._coerce_verdict`).
- **Trivial fast-path** (`triage → doer`) never reaches research; untouched.
- **`gap_eval` LLM exception** → node-level `RetryConfig` (same light
  retry the other branch nodes get), then on exhaustion the gate's
  tolerant parse defaults to sufficient.

## Components touched

| File | Change |
|------|--------|
| `aiforge_core/agents/gap_eval.py` | **new** archetype module |
| `aiforge_core/runtime/parallel_stages.py` | `research_entry` passthrough node + `make_research_entry_node`; export |
| `aiforge_core/runtime/graph_pipeline.py` | `gap_gate`, `ROUTE_RESEARCH_GAP`, `ROUTE_RESEARCH_OK`, `MAX_GAP_PASSES`, `make_gap_gate` |
| `aiforge_core/runtime/pipeline.py` | build `gap_eval`; rewire edges (enhancer→research_entry→branches; merge_context→gap_eval→gap_gate→{planner, research_entry}); single_turn mode + retry_config for gap_eval; guard with `skip_researcher` |
| `aiforge_core/runtime/prompts_extended.py` | `GAP_EVAL` prompt + `{research_gap_brief_md?}` block in `RESEARCHER` |
| `aiforge_core/config/roles.py` + `agents.yaml` | register `gap_eval` role + model assignment |
| `tests/` | gap_gate routing (gap→ok cap), parse-failure→sufficient, skip_researcher omits nodes, prompt-block rendering |

## Model decision

`gap_eval` runs on the **local** model (`build_litellm_model`), not
Claude-pinned. It is a cheap completeness judge feeding a bounded loop,
not a final arbiter — Claude pinning is reserved for Enhancer/Validator.

## Testing

- Unit: `gap_gate` routes `research_gap` when `sufficient:false` &
  count<1; routes `research_ok` when sufficient or count==1.
- Unit: tolerant parse — malformed `gap_verdict` → `research_ok`.
- Unit: `skip_researcher=True` → built graph has no `gap_eval`/`gap_gate`
  nodes and `merge_context`'s out-edge targets `planner`.
- Unit: RESEARCHER prompt renders empty gap block when key absent,
  populated when present.
- Existing pipeline-build + routing tests stay green.

## Out of scope

- Multi-pass research (>1) — capped at 1, consistent with other loops.
- Changing repomap/conventions gatherers.
- Gap analysis on the *plan* (already covered by the verifier loop).
