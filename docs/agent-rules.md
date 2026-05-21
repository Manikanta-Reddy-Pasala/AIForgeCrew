# AIForgeCrew Agent Rules

Per-agent invariants enforced by `aiforge_core/agents/agents.yaml`, the ADK
plugin layer, and runtime guards. Updated as sub-projects land.

## §1 — Defense-in-depth tool filtering

Three independent layers MUST agree:

1. **ADK structural filter** — `agents.loader.tools_schema_for_role()` removes
   non-allowed tools from the model's tool schema before the LlmAgent boots.
2. **GA `tool_before_callback`** — rejects any tool name (allowed or hallucinated)
   that is not in the agent's allowlist at call time.
3. **Trace assertion** — harness pre-flight inspects recorded `:ToolCall` events
   and fails the run if a forbidden name appears.

This means a model that hallucinates "edit_block" while the Doer's allowed set
is `{editor, bash, think, finish, ...}` is stopped at every layer.

## §2 — Memory write isolation

Only Learner writes `:Fact`. Doer NEVER writes memory directly; the
`write_fact` operation is a server-side ADK plugin that fires after Learner
returns its JSON. Memory write attempts from any other role are rejected at
Layer A and logged at Layer C.

## §3 — Scope allowlist globs

Doer mutations resolve to paths inside the active subticket's
`scope_allowlist_globs`. Violations short-circuit through ScopeGuard inside
the tool entry point (`runtime/tools/editor.py`) and never reach disk.

## §4 — Test skeleton injection

Every test subticket MUST reference a template under
`docs/test-skeleton-templates/`. Planner enforces this in its
termination_contract; absence triggers Verifier rejection.

## §X — Tool surface (sub-project #1, 2026-05-21)

**Doer:** `editor`, `bash`, `think`, `finish`, `update_working_checkpoint`,
`graphify_lookup`, `memory_lookup`, `grep_repo`, `fetch_url`, `git_commit`.
All five legacy file/shell tools (`file_read`, `file_write`, `file_patch`,
`list_dir`, `run_shell`, `code_run`) moved to `forbidden`.

**Architect / Planner / Researcher:** `editor` restricted to `editor_commands:
[view]` (view-only). All mutating commands return `editor_command_not_allowed`.

**Verifier / Refiner / Feedback / Learner / Triage:** tool-less
(`forbidden: ALL`) — unchanged.

`finish` is Doer-only and enforced inside the tool (non-Doer attempts return
`agent_not_authorized` regardless of allowlist content).

`bash` sessions are tmux-backed per ADK run; `destroy_session(run_id)` is
called in `runtime/adk_runner._run_pipeline`'s `finally` block so sessions
never leak across tickets. Falls back to stateless subprocess on dev boxes
without tmux.

## Spec history

- `docs/superpowers/specs/2026-05-21-tool-surface-upgrade-design.md` — sub #1
- `docs/superpowers/specs/2026-05-21-openhands-parity-roadmap.md` — subs #1-#9
