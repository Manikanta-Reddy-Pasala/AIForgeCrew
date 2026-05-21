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

A model that hallucinates `edit_block` while the Doer's allowed set is
`{editor, bash, browse, execute_ipython_cell, delegate_to_agent, think, finish, …}`
is stopped at every layer.

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

## §5 — Tool surface (OpenHands parity, 2026-05-21)

**Doer canonical tools** (declared in `agents.yaml.doer.tools.allowed`):

| Tool | Module | Purpose |
|---|---|---|
| `editor` | `tools/editor.py` | view/create/str_replace/insert/undo_edit + per-path snapshot ring depth 5 |
| `bash` | `tools/bash.py` | tmux-backed persistent shell (or Docker via sub #7); cwd/env/jobs persist |
| `browse` | `tools/browser.py` | Playwright headless: goto/screenshot/click/fill/extract_text/mouse_click/key_press/type/scroll/close |
| `execute_ipython_cell` | `tools/ipython_kernel.py` | jupyter_client KernelManager; AgentSkills helpers auto-loaded (sub #12) |
| `delegate_to_agent` | `tools/delegation.py` | spawn single-agent ADK runner; depth-capped via `AIFORGE_DELEGATION_MAX_DEPTH` (sub #16) |
| `mcp` | `tools/mcp_client.py` | JSON-RPC client for FastMCP servers; defaults to oneshell-mcp QA tier (sub #11) |
| `think` | `tools/cognition.py` | no-op + `:Think` trace; 4 KB cap |
| `finish` | `tools/cognition.py` | Doer-only explicit termination signal |
| `grep_repo` | `doer_tools.py` (legacy) | rg/grep wrapper |
| `fetch_url` | `doer_tools.py` (legacy) | http(s) GET only |
| `git_commit` | `doer_tools.py` (legacy) | milestone snapshots inside ticket branch |
| `memory_lookup` | `runtime/memory_lookup_tool.py` | hybrid AiForgeMemory recall (read-only) |
| `graphify_lookup` | `runtime/graphify_lookup_tool.py` | graph traversal (read-only) |
| `update_working_checkpoint` | runtime/learner_persist.py | mid-run state save |

**Architect / Planner / Researcher** carry `editor` with
`editor_commands: [view]` — view-only enforced inside `editor.py`.

**Verifier / Refiner / Feedback / Learner / Triage** are tool-less
(`forbidden: ALL`).

`finish` is Doer-only and enforced inside the tool (non-Doer attempts return
`agent_not_authorized` regardless of allowlist content). Legacy tools
`file_read`/`file_write`/`file_patch`/`list_dir`/`run_shell`/`code_run` are
moved to Doer's `forbidden` list; the underlying functions still ship in
`doer_tools.py` as escape hatches for hallucinated names for one release.

## §6 — Session lifecycle

Every per-run runtime resource is created lazily on first call AND destroyed
in `runtime/adk_runner._run_pipeline`'s `finally` block:

| Resource | Create | Destroy |
|---|---|---|
| tmux bash | `bash._create_session(run_id)` | `bash.destroy_session(run_id)` |
| Playwright browser context | `browser._get_context(run_id)` | `browser.destroy_context(run_id)` |
| IPython kernel | `ipython_kernel._start_kernel(run_id)` | `ipython_kernel.destroy_kernel(run_id)` |
| Docker sandbox container | `docker_sandbox.ensure_container(run_id)` | `docker_sandbox.destroy_container(run_id)` |
| Editor undo ring | per-mutation snapshot | retained 5-deep per path |

`run_id` = ADK `session.id` so the four resources share a single key per
ticket. All cleanups are best-effort: failures are logged at debug and
never block the runner from finalising the ticket.

## §7 — Optional deps & graceful degradation

Each new capability ships with an `*_available()` probe:

| Capability | Probe | Behaviour when absent |
|---|---|---|
| tmux | `bash._tmux_available()` | falls back to stateless subprocess + warn-once trace |
| Playwright | `browser._playwright_available()` | `browse` returns `{ok: False, error: "playwright_missing"}` |
| jupyter_client | `ipython._jupyter_available()` | `execute_ipython_cell` returns `kernel_missing` |
| Docker daemon | `docker_sandbox.is_enabled()` (env + binary + `docker info`) | `bash` runs on host as before |

This way the same code runs on a contributor box (no extras) and the NUC
(everything installed) without conditional imports leaking through to the
agent prompt.

## §8 — Pipeline auto-wires

`_build_prompt` and `_build_context_plugins` integrate the orthogonal
sub-projects into the ADK pipeline without each tool needing its own
plumbing:

| Sub | Wire | Trigger |
|---|---|---|
| #4 condenser | `ContextFilterPlugin.custom_filter` | `AIFORGE_CONDENSER_STRATEGY=amortized\|recent\|llm` |
| #5 microagents | `_build_prompt` prepends matched playbooks | match ticket title+body against `~/.aiforge/microagents/*.md` triggers |
| #6 vision | `_run_pipeline` rewrites first user Content with image parts | ticket has image attachments AND Doer model on vision allowlist |
| #9 budget | `EscalatingLlm.generate_content_async` records per-call usage | every LLM call writes a `Spend` to the in-process tracker |

## §9 — Extended subs #11..#17 (2026-05-22)

Additional OH-parity surfaces beyond the original 1-10:

| Sub | Module | Purpose |
|---|---|---|
| #11 MCP client | `tools/mcp_client.py` | call oneshell-mcp or arbitrary MCP servers; defaults to NUC QA tier |
| #12 AgentSkills | `tools/agentskills.py` | OH-style helpers (open_file/goto_line/find_file/search_dir/...) auto-injected into the IPython kernel |
| #13 Truncation marker | `tools/truncation.py` | `<truncated bytes_dropped=N>` suffix on capped outputs |
| #14 :Condensation event | `condensers.py` | trace node emitted whenever condense() reduces count |
| #15 Trajectory JSON | `trajectory.py` | dump session events+state to `~/.aiforge/trajectories/<ticket>/<run>.json` |
| #16 Delegation depth cap | `tools/delegation.py` | `AIFORGE_DELEGATION_MAX_DEPTH` (default 3) guards runaway recursion |
| #17 Repo microagents | `microagents.py` | `type: repo` files load with no trigger requirement and always inject |

## §10 — Resolver (sub #10, autonomous issue-watcher)

`runtime/resolver.py` polls a GitHub repo for issues tagged `aiforge-bot`
and converts each into a ticket on the AIForge Postgres queue. The existing
`adk_runner` then processes those tickets normally and produces a PR via
`runtime/git_pr.py`. Designed for systemd timer execution.

Env knobs: `AIFORGE_RESOLVER_GH_REPO=owner/repo`,
`AIFORGE_RESOLVER_LABEL=aiforge-bot`, `AIFORGE_RESOLVER_INTERVAL=60`,
`GITHUB_TOKEN`.

## Spec history

- `docs/superpowers/specs/2026-05-21-tool-surface-upgrade-design.md` — sub #1
- `docs/superpowers/specs/2026-05-21-sub2-browser-tool.md` — sub #2
- `docs/superpowers/specs/2026-05-21-sub3-ipython-kernel.md` — sub #3
- `docs/superpowers/specs/2026-05-21-sub4-memory-condenser.md` — sub #4
- `docs/superpowers/specs/2026-05-21-sub5-microagents.md` — sub #5
- `docs/superpowers/specs/2026-05-21-sub6-vision.md` — sub #6
- `docs/superpowers/specs/2026-05-21-sub7-docker-sandbox.md` — sub #7
- `docs/superpowers/specs/2026-05-21-sub8-delegation.md` — sub #8
- `docs/superpowers/specs/2026-05-21-sub9-budget-tracker.md` — sub #9
- `docs/superpowers/specs/2026-05-21-openhands-parity-roadmap.md` — full roadmap + status
- `docs/superpowers/specs/2026-05-22-subs11-17-extension.md` — subs #11..#17 batch
