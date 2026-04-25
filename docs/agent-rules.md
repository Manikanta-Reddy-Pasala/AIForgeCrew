# Agent Rules — AIForgeCrew v5

Companion document to [`aiforge_core/agents.yaml`](../aiforge_core/agents.yaml).
This file explains *why* each per-role contract exists, *where* it is enforced,
and *what* "done" looks like for each role.

If you change `agents.yaml`, update the relevant section here in the same
commit. The two files are a pair.

---

## 1. Why these rules exist

Every constraint in `agents.yaml` traces back to a concrete failure observed
during F1–F7 evals. The point of the rules is structural: stop the model from
making the same class of mistake again, instead of patching prompts after the
fact.

### 1.1 Doer — `ask_user` forbidden

In F1 and F4 the Doer agent, when uncertain, defaulted to `ask_user("which
file should I edit?")`. There is no human in the loop at Doer time — the
ticket *is* the question. Removing `ask_user` from the Doer tool list forces
the agent to either (a) read the repo, (b) call `ask_explorer` for a
read-only delegated question, or (c) terminate with `verdict=fail` and let
Feedback escalate. Empirically this drops "stuck" turns to zero.

### 1.2 Planner — no `edit_block` / `write_file`

In F2 and F5, when the Planner ran on a smolagents CodeAgent backend with the
full Doer tool set, it occasionally drifted into writing the implementation
itself ("smolagents-monolithic-spec drift"). One turn produced a 600-line
plan plus a half-written patch. Planner cannot edit code at all; the
structural fix is to never expose the edit tools.

### 1.3 Doer — ScopeGuard via subticket allowlist globs

F6 and F7 surfaced repeated scope violations on `PaymentInRepository.java`
and adjacent files: a Doer assigned to fix `SaleOrderService` would also
"helpfully" rewrite `PaymentInRepository` because both appeared in the
context bundle. Every subticket now carries an explicit `scope_allowlist_globs`
list, and `code_run` / `file_write` / `file_patch` are wrapped by ScopeGuard.
Any write outside the allowlist is rejected at call time and surfaces as
`verdict=scope_violation` from Feedback.

### 1.4 Test class authoring (F7c)

F7c showed Planners producing test subtickets that read "add tests for X" and
nothing else. The Doer would then either skip tests or invent a test harness
that didn't match the existing project layout. The v4 fix recipe — Planner
must inject a test-skeleton-template reference into every test subticket — is
now mandatory and lives in §4 below.

---

## 2. Where each rule is enforced

Rules are enforced in three layers, top to bottom. A bug in any one layer is
caught by the others.

### Layer A — ADK side: `tools=[...]` filter

When `aiforge_core/agents.py:load_agents()` constructs each ADK agent, it
passes only the `tools.allowed` list to the agent constructor. The model
literally never sees the schema for forbidden tools — it cannot emit a tool
call for them because it does not know they exist. This is the cheapest and
strongest layer.

### Layer B — GA handler: `tool_before_callback` reject list

The custom GenericAgent text-protocol handler (used by the Doer) parses
free-form `<tool_use>...</tool_use>` blocks from the model output. Because
that channel is unfiltered, a model could in principle emit a forbidden tool
name (e.g. by hallucination, or by piping through `code_run "ask_user ..."`).
The `tool_before_callback` checks the resolved tool name against the
agent's `tools.forbidden` list and aborts the turn with an explicit error.

### Layer C — Harness: pre-flight trace assertion

After a run completes, the eval harness walks the recorded trace and asserts
that no event references a forbidden tool name for the agent that emitted it.
This is a backstop: if Layers A and B are bypassed (e.g. by a misconfigured
plugin during local dev), the harness fails the run loudly instead of
silently green-lighting it.

---

## 3. Termination contracts

A role is "done" only when its `termination_contract` in `agents.yaml` is
satisfied. If the contract cannot be met within `max_turns` / `max_wall_s`,
the agent escalates to Feedback with a fail verdict.

| Role      | "Done" looks like                                                                                  | Escalation trigger                                            |
|-----------|----------------------------------------------------------------------------------------------------|---------------------------------------------------------------|
| Architect | Parent ticket persisted, acceptance_criteria attached, human closes turn                           | Human cancels                                                 |
| Planner   | `plan_md` written, ≥1 child subticket per AC, each with allowlist globs and (for tests) a skeleton | No subticket created OR subticket missing allowlist           |
| Doer      | Compile green AND tests green, all writes inside allowlist                                         | 2 consecutive compile fails OR scope violation OR turn budget |
| Feedback  | Valid JSON verdict in {pass, fail, scope_violation} with non-empty rationale                       | n/a (single-shot, never escalates)                            |
| Learner   | `facts_json` validates and server-side `write_fact` plugin acks                                    | Skipped entirely if verdict ≠ pass                            |

The graph runner reads termination_contract status off the trace and routes
control accordingly. Doer's "2 consecutive compile failures" recovery rule is
implemented in `aiforge_core/doer/acceptance_gate.py` and surfaces as a fail
verdict from Feedback rather than as Doer self-termination — see §8.

---

## 4. Test class authoring rule (F7c)

> **Rule:** Every test subticket emitted by Planner MUST carry a
> `test_skeleton_ref` pointing to a template under
> `docs/test-skeleton-templates/`.

This was the F7c v4 fix recipe. Without a skeleton, Doer either:

- skips writing tests entirely, or
- invents a JUnit / pytest layout that doesn't match the surrounding project.

The skeleton is a small file (10–30 lines) with the project's preferred
package, imports, base test class, and a single `@Test` method whose body is
`// TODO: implement`. Doer fills in the body and adds adjacent test methods.

> **TODO:** `docs/test-skeleton-templates/` does not yet exist. Create the
> directory with at least:
> - `java-junit5-spring-webflux.template.java`
> - `java-junit5-mongo-reactive.template.java`
> - `python-pytest-flask.template.py`
> - `react-vitest-component.template.tsx`
>
> Until the directory exists, Planner SHOULD still emit a `test_skeleton_ref`
> field naming the intended template, and Doer falls back to the closest
> existing test in the repo. The `test_skeleton_ref` field is mandatory in the
> subticket schema regardless.

---

## 5. Memory-write rule

> **Rule:** Only Learner writes `:Fact` nodes, and only when
> `feedback.verdict == "pass"`. Doer NEVER writes memory.

This is enforced by the ADK plugin layer, not by the Doer's tool list alone:

- Doer has no `write_fact` tool exposed (Layer A).
- The `write_fact` plugin checks `agent_role == "learner"` AND
  `current_verdict == "pass"` before persisting (additional gate).
- Learner cannot bypass this either: even though it is the legal writer, the
  plugin refuses on `verdict in {fail, scope_violation, None}`.

The reason memory writes are isolated to one role on one signal is to keep
the fact corpus high-precision. F4 showed that letting Doer write facts
mid-implementation poisoned the corpus with "I tried X and it failed" notes
that later Planners mistook for canonical guidance.

---

## 6. Auto-remember rule (`:Turn` nodes)

> **Rule:** Every turn for every agent writes a `:Turn` node via the ADK
> `BasePlugin.on_event_callback`. This is not opt-out and not configurable
> per agent.

`:Turn` is distinct from `:Fact`:

- `:Turn` = raw event log of what happened, scoped to the run, never used for
  retrieval into future runs. Lives in Postgres `runs.turns`.
- `:Fact` = curated, durable, searchable. Lives in the memory store and
  feeds future agents via `search_memory`.

Auto-remembered turns power post-hoc analysis, eval replays, and the GA-style
result auto-shrink path (commit `f837121`). Because they're cheap and never
queried at agent time, there's no reason to disable them.

---

## 7. Forbidden-tool list (mirror of `agents.yaml`)

This table mirrors `agents.yaml` for at-a-glance review. If the two
disagree, `agents.yaml` is the source of truth.

| Role      | Forbidden tools                                                                       |
|-----------|---------------------------------------------------------------------------------------|
| Architect | `edit_block`, `write_file`, `file_patch`, `run_compile`, `file_write`, `code_run`     |
| Planner   | `edit_block`, `write_file`, `file_patch`, `run_compile`, `ask_user`, `code_run`       |
| Doer      | `ask_user`, `start_long_term_update`, `create_child_ticket`, `write_fact`, `write_plan` |
| Feedback  | `ALL` (no tool calls permitted)                                                       |
| Learner   | `ALL` (write_fact is a server-side plugin, not a model-callable tool)                 |

The keyword `ALL` is interpreted by the GA handler as a blanket reject: any
`tool_use` block, regardless of name, is rejected before dispatch.

---

## 8. Recovery rules

### 8.1 Doer — 2 consecutive compile failures

> **Rule:** On 2 consecutive compile failures within a single Doer turn, the
> turn must halt with `verdict=fail`. Doer does not decide this itself —
> the failure is observed by the acceptance gate
> (`aiforge_core/doer/acceptance_gate.py`) and surfaces as a fail verdict
> from Feedback.

Empirically (F2, F6) once compile breaks twice in a row the model spirals:
each "fix" introduces a new compile error somewhere else, and the next 10–20
turns are spent thrashing. Halting at 2 lets Feedback diagnose, lets Learner
skip (no fact written), and lets the next Planner re-decompose if needed.

### 8.2 Doer — scope violation is terminal

A single `file_write` / `file_patch` / `code_run` write outside the allowlist
globs ends the turn immediately with `verdict=scope_violation`. There is no
"warn and continue" path. ScopeGuard rejects the write at the tool layer, the
Doer sees the rejection in its turn log, and Feedback's verdict is
deterministic: `scope_violation` outranks any test status.

### 8.3 Learner — schema-validation retry once

If `facts_json` fails schema validation, Learner re-runs once with the
validation error fed back as user message. If it fails twice, the run is
marked "passed but unlearned" — the implementation ships, but no facts are
written. This avoids the failure mode where a malformed Learner output
blocks an otherwise-green Doer turn from being merged.

### 8.4 Planner — missing allowlist on a child subticket

If Planner emits a child subticket without `scope_allowlist_globs`, the
graph runner rejects it before scheduling Doer and asks Planner to repair.
Two repair attempts; on the third, parent ticket is marked `BLOCKED` and
Architect is notified. This is structural — Doer cannot run without an
allowlist, so an unrepaired subticket is a dead-end.

---

## Cross-references

- `aiforge_core/agents.yaml` — machine-readable contracts
- `aiforge_core/agents.py:load_agents()` — loader and ADK construction
- `aiforge_core/doer/acceptance_gate.py` — compile/test gate that drives §8.1
- `aiforge_core/graph/edges.py` — verdict routing between roles
- `docs/test-skeleton-templates/` — TODO; see §4
- `docs/architecture.md` — system-level overview of the v5 pipeline
- F1–F7 eval fixtures under `evals/fixtures/` — empirical motivation for the
  rules in §1
