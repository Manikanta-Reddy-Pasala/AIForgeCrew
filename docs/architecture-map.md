# Architecture map — one place to look per concern

After the drift-unify pass, most cross-cutting concerns have a **single source**.
This is the index: "where do I change X?" Anything not listed is local to its
feature module.

## Single source of truth (change it in ONE place)

| Concern | The one module | Notes |
|---|---|---|
| **Context assembly** (rules + prefs + skills + workflows + memory + repo map, scoped & query-gated) | `aiforge_core/runtime/context_bundle.py` → `build_bundle()` | Both chat + chat-team route through it. Wraps the leaf helpers; don't re-gather context anywhere else. |
| **"Which repo am I"** (repo key/name) | `aiforge_core/runtime/repo_ident.py` → `repo_name(cwd, sentinel)` | git-toplevel basename. `_chat_repo_key`, `chat_agent._repo_name`, `skills._repo_name` all delegate. |
| **Integration config** (jira/confluence/gitlab base_url/token/insecure/defaults) | `aiforge_core/runtime/tools/_http_integration.py` → `integration_conf()` | Each tool's `_conf()` is a 3-line call. Insecure-TLS-by-default policy lives here. |
| **Background work** (daemon thread / detached process) | `aiforge_core/runtime/background.py` → `spawn()` | Enforces name + error sink + process-vs-thread. Migrate remaining hand-rolled `Thread`/`Popen` here. |
| **Capture cue gate** ("is this a preference/directive?") | `aiforge_core/runtime/capture_cues.py` → `has_cue()` | The single regex all capture paths share. |
| **Rules store** (create / list / delete / read) | `aiforge_core/runtime/repo_rules.py` | Canonical store (`~/.aiforge/rules` + repo-local). Library UI, `remember_rule`, and `rule_capture` all write here; `_rules_context` + the pipeline read it. |
| **Preferences** | sqlite `pref:` units (write: `preference_capture` → `sqlite_memory.upsert_by_tag`; read: `chat_agent._preferences_context`) | Merged into the doer's `user_prefs_md` too. |
| **Repo → local folder** | `aiforge_core/config/repo_map.py` (`repos.json`) + `workspace.resolve_repo_dir()` | Global base + per-repo paths. |
| **Scheduled jobs** (cron) | `aiforge_core/jobs/scheduler.py` | at-most-once via `store.claim`. The reference design for any periodic work. |
| **Code index / re-index** | `aiforge_core/runtime/memory_ingest.py` (`run_index`, `reindex_all`) | Daily sweep + `POST /api/memory/reindex-all`. |

## Still multiple places — by design or guarded (know before you touch)

| Concern | Where | Why it's split |
|---|---|---|
| **Agent execution engine** | `chat_agent.run_chat_agent` (ReAct text protocol) **+** `adk_runner` (ADK native function-calling) | INTENTIONAL — local models use the text protocol, cloud/ADK models use native tool-calls. A fix to `run_chat_agent` already propagates to chat/plan/parallel/text_doer (they all call it). |
| **Tool registry** | `chat_agent.TOOLS` (`handler(args,cwd)→dict`) **+** `doer_tools` (typed `FunctionTool`) | Two calling conventions. NOT merged — the drift ("a tool in one surface not the other") is **guarded** by `tests/python/runtime/test_chat_tool_parity.py::test_cross_surface_tools_in_both_registries`. Add a cross-surface tool to BOTH + the guard's list. |
| **Post-turn capture** | `chat_learner` (distil facts) · `preference_capture` (subject-upsert prefs) · `rule_capture` (pre-agent directive classify) | Different purposes/timing. Unified the *gate* (`capture_cues`) and de-duped the double-write (a preference turn skips the fact-distiller). |
| **Memory backends** | `sqlite_memory` (embedded) · Neo4j/AFM (pro) | `backend_select.embedded()` picks. Layers B (tree-sitter symbols) + C (graphify) are Neo4j-only. |

## Rules of thumb
- Adding an **agent tool** → both `chat_agent.TOOLS` AND `doer_tools.__all__` (+ the parity-guard list if cross-surface).
- Adding a **context source** → `context_bundle.build_bundle()`, nowhere else.
- Needing the **repo key** → `repo_ident.repo_name()`. Never re-derive a basename.
- Any **background task** → `background.spawn()`; CPU-bound → `kind="process"`.
- A new **integration** → reuse `integration_conf()`.
