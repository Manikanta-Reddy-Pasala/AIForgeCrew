# Rule/Skill/Workflow Context Gating + Disambiguation — Design

**Date:** 2026-07-01
**Status:** Approved (design), pending implementation plan

## Problem

Three related gaps reported by the user across simple/plan/team chat modes:

1. **Noise.** `chat_agent._rules_context()` (simple/plan mode) injects EVERY
   remembered rule bullet into EVERY turn, unconditionally — no topic or
   scope gating at all. Irrelevant rules burn prompt budget and can bleed
   into unrelated answers.
2. **Silent ambiguity.** `skills.py`/`workflows.py` already auto-select by a
   shared trigger/token-overlap scorer (`skills.search()` — `workflows.py`
   reuses it verbatim), but when two or more candidates score close, the
   system silently picks one instead of asking which was meant (the
   "which workflow?" case).
3. **Fragmented rule storage.** Two separate rule stores exist —
   `repo_rules.py` (file-based `.aiforge/rules/*.md`, glob-scoped, team/
   pipeline only) and `chat_agent._rules_context()` (memory-store bullets
   captured via `rule_capture.classify()` per the approved
   [Rule/Memory/Feedback Capture design](2026-06-26-rule-memory-capture-design.md),
   always-on, simple/plan only) — with no shared gating mechanism between
   them.

### Today

- `skills.py.search()` — the one shared trigger-scorer; `always=true` skills
  bypass scoring (capped at `AIFORGE_SKILLS_ALWAYS_CAP`, default 8).
- `workflows.py.search()` = `skills.search()` called against the workflow
  pool — same scorer, same dataclass (`Skill`), separate folder.
- `repo_rules.py` — deterministic, zero-LLM, glob-only matching
  (`alwaysApply:true` / no globs = always; else fnmatch-intersect against
  ticket scope globs). No trigger/topic concept.
- `chat_agent._rules_context()` — dumps every `rules:global` +
  `rules:{repo}` memory-store bullet into every session turn, no gating.
- Disambiguation surfaces that already exist and can be reused as-is:
  - simple/plan chat has a live `ASK:` protocol
    (`chat_agent.py:1273` — "if it's ambiguous... ASK your clarifying
    questions UP-FRONT... don't guess").
  - team/pipeline **interactive** tickets (`metadata.interactive=True`,
    chat-originated) already run `clarify.py:maybe_clarify()` pre-pipeline:
    LLM checks clarity, halts via `metadata.awaiting_input` +
    `clarify_questions`, resumes via `POST /api/tickets/{id}/answer`.
  - team/pipeline **autonomous** tickets (no `interactive` flag) run
    unattended — must never block.

## Decisions (from brainstorming)

- Fix noise AND silent ambiguity together, not separately.
- Unify skills/workflows/rules disambiguation on ONE shared scorer rather
  than three near-duplicate implementations.
- Gating method: **hybrid** — deterministic trigger match first (fast,
  free, consistent with `repo_rules.py`'s existing "zero LLM cost"
  philosophy); LLM classify only as a fallback when scores are ambiguous
  (mirrors the existing `turn_router.py` hybrid pattern: deterministic
  first, cheap LLM only when needed).
- Disambiguation surfacing is **mode-dependent**: block in simple/plan
  (live user) and interactive team/pipeline tickets (async round-trip
  already exists); never block autonomous tickets — best-guess + visible
  notice instead (mirrors `turn_router.py`'s "Small follow-up" notice
  pattern).
- Reuse existing plumbing wherever it already exists (`ASK:` protocol,
  `clarify.py`, ticket event trace) — no new UI, no new endpoints.

## Architecture

### 1. Shared ambiguity-aware scorer (`skills.py`)

Extend the existing scorer rather than forking it:

```
skills.select_or_ask(query, cwd, k, pool=...) -> (chosen: list[Skill], ambiguous: list[list[Skill]])
```

- Runs `search()` as today to get scored candidates.
- **Ambiguity rule:** within the top-scoring group, if candidate[1]'s score
  is within `AIFORGE_AMBIGUITY_MARGIN` (default `0.15`, i.e. top-2 within
  15% of top-1's score) AND candidate[1]'s score clears a noise floor
  (avoids two near-zero garbage matches falsely tying), the group is
  returned as `ambiguous` instead of being silently auto-picked.
- `always=true` / `alwaysApply` items bypass scoring entirely — unchanged
  escape hatch.
- `workflows.py` and the new rules-gating path (below) both call this same
  function. One scorer, three consumers.
- `selected_names()`-style reporting (drives the existing "skills/workflows
  used" UI badge) extended to `why: always | match | ambiguous` everywhere,
  including the new rules paths.

### 2. Gate rules by the same scorer

**`repo_rules.py`** (file-based, team/pipeline): add optional `triggers:`
frontmatter alongside existing `globs`/`alwaysApply`. A rule fires if EITHER
its globs match the ticket's scope globs (as today) OR its triggers score
via `skills.search()` against a `Rule` → `Skill`-shaped adapter.
`alwaysApply:true` still bypasses everything.

**`chat_agent._rules_context()`** (memory-store bullets, simple/plan):
currently the actual noise source — every bullet, every turn. Per the
approved [rule-capture design](2026-06-26-rule-memory-capture-design.md),
rules are captured via `rule_capture.classify()`'s LLM pass. Extend that
classify call's output schema with an optional `triggers` list (the
classifier already reads the message; inferring 1-3 topic words is a
natural extension of `canonical`/`scope`, not a new LLM call). Rules
without inferred/tagged triggers (legacy bullets captured before this
change) default to `always=true` — **backward compatible, nothing silently
drops for existing users**. Tagged rules go through `select_or_ask()` same
as skills/workflows.

### 3. Disambiguation wiring, mode-dependent

No new plumbing — inject ambiguity info into surfaces that already ask:

- **simple/plan chat:** when `select_or_ask()` returns an ambiguous group
  for skills/workflows/rules, inject an `AMBIGUOUS MATCH` note into the
  system prompt next to the skills/workflows/rules blocks, e.g. *"Rules
  'deploy-staging' and 'deploy-prod' both matched — ASK which one applies
  before proceeding."* The agent's existing `ASK:` behavior does the rest.
  Blocking (live user).
- **team/pipeline, interactive tickets:** extend `clarify.py:_ask_llm()`'s
  user message to also list any ambiguous skill/workflow/rule candidates
  as extra signal for the existing clarity check. Same halt
  (`awaiting_input` + `clarify_questions`) / resume (`/answer`) path,
  unchanged.
- **team/pipeline, autonomous tickets:** never block. Best-guess = highest
  `priority` (already a `Skill` field) among the tied group, ties within
  equal priority broken by score — an ambiguous group is by definition
  already near-tied on score, so an operator's explicit priority is the
  more meaningful signal than noise in the last few score points. Emit a
  non-blocking notice event on the ticket's trace, e.g. *"Matched
  'deploy-staging' over 'deploy-prod' (ambiguous, picked highest-priority)
  — say so if wrong."* — same pattern as `turn_router.py`'s existing
  "Small follow-up" thought bubble.

## Testing

- `skills.select_or_ask()`: clear-winner (no ambiguity), near-tie
  (ambiguity fires), always-on bypass, noise-floor (two near-zero scores
  don't falsely tie).
- `repo_rules.py`: `triggers:` frontmatter parsing, OR-gate with globs
  (either fires → rule applies), `alwaysApply` bypass unchanged.
- `chat_agent._rules_context()`: untagged legacy rule defaults to
  always-on (no regression), tagged rule gated by score, ambiguous pair
  injects the `AMBIGUOUS MATCH` block.
- `clarify.py`: ambiguous-candidate context reaches `_ask_llm`'s user
  message; existing halt/resume path untouched otherwise.
- Autonomous-ticket best-guess: never emits `awaiting_input`; always emits
  the non-blocking notice event when ambiguous.

## Rollout

- Flag: `AIFORGE_AMBIGUITY_MARGIN` (default `0.15`). Setting it to `0`
  disables ambiguity detection entirely (never ambiguous — old silent-pick
  behavior), consistent with every other feature flag in this codebase.
- Single PR, no migration needed — all new fields are optional with
  backward-compatible defaults (untagged rule = always-on, globs-only rule
  unaffected, no schema change required for existing `.aiforge/rules/*.md`
  or memory-store bullets).

## Out of scope

- Unifying the two rule storage backends (file-based vs memory-store) into
  one store — orthogonal to gating; not needed to fix noise/ambiguity.
- A "mini" chat mode — confirmed not to exist in the backend (only
  `simple`, `plan`, `team`); no work needed there.
