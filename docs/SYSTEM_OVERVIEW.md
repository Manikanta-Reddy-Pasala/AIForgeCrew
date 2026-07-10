# AIForgeCrew — System Overview

Audience: the operator and new team members. Every claim below is verified
against the code (file references inline). Companion docs:
[QUICKSTART.md](../QUICKSTART.md) (setup) · [TOOLS.md](TOOLS.md) (complete
tool reference + per-agent allowlists) · [DECISIONS.md](DECISIONS.md) (why
things are the way they are) · [DEMO_GUIDE.md](DEMO_GUIDE.md) (walkthrough).

---

## 1. What this is

AIForgeCrew is an autonomous AI dev team that runs on your own hardware.
It has two faces: a conversational coding agent (chat UI, full filesystem
access) and a ticket→PR pipeline (enhancer → architect → parallel builders →
reconcile → PR). Everything works with fully local models — the agent speaks
a plain-text protocol, so any OpenAI-compatible endpoint works, no native
tool-calling needed. Deploys anywhere with one command: `./run.sh`.

---

## 2. How a request flows

### 2a. Chat, simple mode

1. You send a message. The server (`aiforge_core/api/api.py`) routes it to the
   chat agent (`aiforge_core/runtime/chat_agent.py`).
2. Context is assembled in a fixed order (`aiforge_core/runtime/context_bundle.py`):
   preferences → **rules (injected every turn, always)** → project brief →
   skills → **workflows (mandatory procedures — injected before the repo map
   and never dropped, even in low-context "cave" mode)** → repo summary →
   AST repo map → memory recall.
3. A multi-part message ("fix X. also why Y? and add Z") gets a derived
   **checklist** pinned into the context; the agent flips items live via the
   `plan_progress` tool (`chat_agent.py`, `_derive_checklist` area ~line 3444).
4. The model runs a ReAct loop speaking a **text protocol** — each turn is
   `THOUGHT:` + `ACTION: <tool>` + `ARGS_JSON: {...}`, or `FINAL: <answer>`.
   No native tool-calling, so it works on any backend (LM Studio, vLLM,
   OpenRouter, cloud).
5. Tools execute; risky ones pause for approval (gating rules: [TOOLS.md](TOOLS.md)).
6. On `FINAL` for a multi-part ask, a one-time **completeness gate** makes the
   model self-check its answer against the checklist before the answer is
   accepted (`chat_agent.py` ~line 4029).
7. **Auto-escalation:** a multi-file BUILD request in simple mode routes into
   the pipeline instead (`api.py` ~line 3995). Plan mode is read-only and
   never escalates.

### 2b. Pipeline (team mode / tickets)

Runs for `mode: team`, tickets, and escalated chat builds
(`aiforge_core/runtime/pipeline.py` + `parallel_subtasks.py`).

1. **Enhancer** rewrites the raw ask into a spec. A **degenerate-output
   guard** restores the raw ask if the rewrite collapsed or lost every named
   file/symbol (`pipeline.py: _make_enhancer_guard`,
   `parallel_subtasks.py: _spec_degenerate`).
2. **Architect** emits a file plan. A deterministic **plan gate** validates it
   (file dump, missing tests, mixed languages …) and gives the model exactly
   **one semantic reask**; a still-broken retry ships the sanitized plan
   (`parallel_subtasks.py: _validate_plan`, ~line 1520).
3. If the plan has no test files, a **test backstop** adds a unit-test subtask
   per code module (`_ensure_test_coverage`).
4. **SPEC.md is always written** to the workspace before any subtask runs —
   the shared contract every worker builds against (~line 1800). Mid-run
   steering is appended to it.
5. Subtasks run **in parallel (default ON, max 4 workers)**, each in its
   **own git worktree** with a fresh context: only its goal + the right
   SPEC.md slice. **Test subtasks are built first** (test-first, ~line 1920);
   per-subtask validation is compile/build.
6. Successful branches **merge sequentially** into the ticket branch.
7. A fresh-context **spec verification** pass reads SPEC.md + the produced
   tree and confirms every requirement was addressed (`_verify_against_spec`).
   Off-plan phantom files are pruned.
8. **Reconcile loop** compiles + tests the merged tree and fixes cross-file
   drift until green. Inside it: a **config-validity gate** (a broken
   pyproject/build file is fixed first — nothing can run until it parses,
   ~line 3419), **escalation** of a stuck residual to a stronger model
   (`AIFORGE_ESCALATION_MODEL`, ~line 3445), and a **test-audit** (a wrong
   test assertion may be corrected, marked with a `# test-audit:` comment).
9. A PR is opened (`git_pr.py`) with an honest verdict (green / some tests
   fail / couldn't run here).

---

## 3. Tool surface

One line per group. The complete per-tool reference (args, gating, which
agent gets what) is **[TOOLS.md](TOOLS.md)**.

| Group | What it gives you |
|---|---|
| Files / edit | read, write, patch, `editor` (str_replace/insert/undo, syntax-checked), atomic `multi_edit` |
| Search / nav | ripgrep, find, glob, AST `repo_map`, `lsp` (goto-def / refs / hover) |
| Code exec / tests | `run_command`, per-test `run_tests`, `typecheck`, `format`, persistent `ipython`, project detect+build/run, background `serve` |
| Git / PR | targeted git via shell, `github_pr`, `gitlab_mr_create` / comment |
| Jira | search/read + create/update/comment, transitions, worklog + `log_work`, boards/sprints/projects, dashboards, remote links (`runtime/tools/jira.py`) |
| Confluence | read/create/update, spaces, page-by-title, labels, comments, descendants (`runtime/tools/confluence.py`) |
| GitLab | issues + MRs (`runtime/tools/gitlab.py`) |
| Email | `email_send` (approval-gated) / `email_read` (`runtime/tools/email_tool.py`) |
| Web | `web_search`, `web_fetch`, `web_crawl` → markdown dossier in `work/web/` (`runtime/tools/web_search.py`, `web_ingest.py`) |
| Memory / learning | `memory_lookup`, `memory_write`, `remember_rule`, skill + workflow search/learn |
| Task tracking | `plan_progress` — live checklist for multi-part asks |
| Resolvers | loose name → real thing: `resolve_repo`, `jira_resolve_project`, `confluence_resolve_space` |
| Scripts | `aiforge-tool <name> '<json>'` CLI — job/workflow scripts call the same tool registry (read-only by default; `runtime/tool_cli.py`) |

---

## 4. Memory & knowledge

- **OKR-DAG** (`aiforge_core/memory/okr/`, see **[OKR_MEMORY.md](OKR_MEMORY.md)**):
  the goal-oriented memory. Markdown nodes in `~/.aiforge/memory/okr/{objectives,
  key_results,learnings,sessions}/` carry typed frontmatter edges
  (`parent_objective`, `linked_krs`, `scope`), built into an **in-memory graph**
  (plain dicts, no DB). *Surgical* retrieval for the active Key Result — ascend
  (objective *why*), descend (KR *what*), constraints (global + scoped learnings),
  recent (last N sessions) — compiles a bounded `<OBJECTIVE>/<ACTIVE_TASK>/
  <CRITICAL_RULES>/<RECENT_ACTIVITY>` block into the prompt. Sessions **auto-author**
  durable Objectives/KRs/Learnings (LLM-verified before save).
- **OKR envelope + topic briefs** (`runtime/work_notes.py`, `memory/md_store.py`):
  every write goes through `capture()` — a tagged unit (by **topic** and the
  **agent** that wrote it) folded **hourly** into topic briefs that dedupe/merge
  via an LLM, **split-on-oversize** into cross-referenced parts, and carry the
  Google-OKR envelope. A **session execution ledger** (`runtime/session_ledger.py`)
  injects "already ran — don't repeat" and auto-captures verified **working
  workflows**.
- **Unified recall** (`aiforge_core/memory/unified_query.py`): one query fans
  out in parallel to all sources — SQLite (embedded) or Neo4j vector/text search,
  ticket brief, graph hops, code-symbol lookup, markdown/SOP docs, external
  library docs — scored, weighted, deduped, top-K. **Code chunks are demoted**
  (`AIFORGE_UMEM_CHUNK_SCORE`, default 0.4) so curated OKR/topic knowledge outranks
  raw RAG. Neo4j is **optional** now — the OKR-DAG is DB-free.
- **Shared work folders** (`runtime/work_context.py`): work about a durable
  thing lives in `~/.aiforge/work/<kind>/<key>/` (`jira/PROJ-123`,
  `confluence/<page>`, `repo/<name>`, `web/`) — shared across every session
  that touches that context. Plain chats stay ephemeral.
- **Chunking** (`packages/aiforge_memory/.../features/chunk/`): code uses our
  **own AST chunker** over `tree_sitter_language_pack`; prose/docs use
  chonkie's text chunkers; any failure falls back to plain line windows —
  ingestion never breaks.
- **Project briefs**: per-repo consolidated memory, injected highest in the
  context bundle; sessions and scopes are compacted into briefs
  (`context_bundle.py`, `chat_summary.py`, `condensers.py`).
- **Graphify graph**: a repo's `graphify-out/graph.json` (community-clustered
  code graph) is loaded into Neo4j by `indexing/graphify_loader.py` and queried
  by the `graphify_lookup` tool (architect/planner/doer/researcher). Refresh:
  `scripts/runtime/aiforge-graphify-all.sh`; install via `run.sh --with-graphify`.
- **Langfuse mirror**: when enabled, every LLM call *and* every memory recall is
  mirrored to the local Langfuse **v2** UI (single container, no ClickHouse),
  fire-and-forget — with **sessions** (per chat) and a per-turn **score**. Input/
  output are set on the trace so the Sessions view isn't blank
  (`integrations/langfuse_adapter.py`, `memory/unified_query.py`).

---

## 5. Skills, workflows & rules

Three kinds of reusable instruction, all plain markdown with one unified
frontmatter (`name` / `description` / `triggers` / `scope`), managed in the
Library UI, authored in chat, or written by the agent itself
(`learn_skill` / `learn_workflow` / `remember_rule`).

| Kind | What | When it fires |
|---|---|---|
| Skill | know-how for a kind of task | relevance match on `triggers` + description |
| Workflow | end-to-end procedure (ordered steps, optional runnable `scripts/`) | relevance match; **mandatory** once matched — survives cave mode |
| Rule | always-on constraint | every turn (`alwaysApply`) or when edited files match its `globs` |

Load order (later wins on a name clash): **builtin**
(`runtime/builtin_playbooks/`) → global (`~/.aiforge/…`) → repo-local
(`<repo>/.aiforge/`, `.claude/`, `.openhands/`). A custom item always
outranks a shipped builtin. Workflow scripts pass a hard run-before-save
test gate (`runtime/workflows.py`).

---

## 6. Where to change what

Single-source seams — change these in ONE place:

| Concern | The one module |
|---|---|
| Context assembly (rules/prefs/skills/workflows/memory/repo-map) | `runtime/context_bundle.py: build_bundle()` |
| "Which repo am I" (repo key) | `runtime/repo_ident.py: repo_name()` |
| Jira/Confluence/GitLab HTTP config | `runtime/tools/_http_integration.py: integration_conf()` |
| Rules store | `runtime/repo_rules.py` |
| Background threads/processes | `runtime/background.py: spawn()` |
| Repo → local folder mapping | `config/repo_map.py` + `runtime/workspace.py` |

Intentionally split (guarded, not merged): the two agent engines
(`chat_agent.run_chat_agent` text protocol vs `adk_runner` native ADK) and
the two tool registries (`chat_agent.TOOLS` vs `doer_tools`) — cross-surface
drift fails a parity test + a startup check (`runtime/tool_manifest.py`).

Adding a cross-surface tool → add to BOTH registries + `tool_manifest.CROSS_SURFACE`.
Adding a context source → `context_bundle.build_bundle()`, nowhere else.

The full decision history (what was chosen, why, evidence, date) lives in
**[DECISIONS.md](DECISIONS.md)**.

---

## 7. Operating it

```bash
./run.sh                 # hybrid (default): infra in Docker, agent on host
./run.sh --docker        # everything in containers (isolated agent)
./run.sh --lite          # ZERO-Docker: SQLite for tickets/chat/jobs + memory
./run.sh --migrate       # move Postgres (chat+tickets) → SQLite, remove DB infra
./run.sh --dev           # uvicorn --reload | --port N | --host H
./run.sh --test          # probe the configured model endpoint
./run.sh --reset-config  # wipe ~/.aiforge/agent_config.json (backed up)
./run.sh --with-langfuse # start the self-hosted trace UI (:3005) — ok in --lite
./run.sh --stop-langfuse # stop it (ephemeral data anyway)
```

Mode is also `AIFORGE_MODE`-driven (`lite`|`hybrid`|`docker`) so a headless service
picks zero-Docker via `.env` without editing its unit. `--lite` + `--with-langfuse`
= tracing is the only container.

Config lives in `.env` (repo root) and `~/.aiforge/` (`agent_config.json`,
`integrations.json`, `langfuse.env`, `work/` folders). Env always wins over
the UI store.

Autonomous intake: `runtime/resolver.py` polls a GitHub repo for issues
labelled `aiforge-bot` and files each as a ticket (cron/systemd-timer friendly;
`AIFORGE_RESOLVER_GH_REPO`, `AIFORGE_RESOLVER_LABEL`, `GITHUB_TOKEN`).

| Env toggle | Default | Does |
|---|---|---|
| `AIFORGE_PARALLEL_SUBTASKS` | on | `0` disables the parallel fan-out |
| `AIFORGE_PARALLEL_SUBTASKS_MAX` | 4 | concurrent subtask workers (1–8) |
| `AIFORGE_ESCALATION_MODEL` | unset | stronger model for a stuck reconcile residual |
| `AIFORGE_CODEMEM_CHUNKER` | ast | code-memory chunker selection |
| `LANGFUSE_HOST` / `_PUBLIC_KEY` / `_SECRET_KEY` | unset | enables trace mirroring (`AIFORGE_LANGFUSE_DISABLE=1` kills it) |
| `AIFORGE_ALLOW_WEB_FETCH` | off | opens the web fetch/crawl egress gate |
| `AIFORGE_ALLOW_DELETE` | off | skips the destructive-delete confirmation |
| `AIFORGE_CAVE_MODE` | auto | force lean context on/off (auto ≤48K window) |
| `AIFORGE_TOOL_POLICY` | — | per-tool overrides, e.g. `run_command=ask` |
