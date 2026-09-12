# AIForgeCrew — System Overview

Audience: the operator and new team members. Every claim is verified against
the code (file references inline). Companion docs:
[QUICKSTART.md](../QUICKSTART.md) (setup) · [TOOLS.md](TOOLS.md) (complete tool
reference + per-agent allowlists) · [DECISIONS.md](DECISIONS.md) (why things
are the way they are) · [OKR_MEMORY.md](OKR_MEMORY.md) (memory) ·
[OKF.md](OKF.md) (the on-disk memory format).

---

## 1. What this is

An autonomous AI dev team that runs on your own hardware. Two faces: a
conversational coding agent (chat UI, full filesystem access) and a ticket→PR
pipeline (enhancer → architect → parallel builders → reconcile → PR). It works
with fully local models — the agent speaks a plain-text protocol, so any
OpenAI-compatible endpoint works, no native tool-calling needed. One command:
`./run.sh`.

---

## 2. How a request flows

### 2a. Chat, simple mode

1. The server (`api/routes/chat.py`) routes the message to the chat agent
   (`runtime/chat_agent/`, a package: `_loop` · `_registry` · `_prompt` ·
   `_tools/` · `_shell` · `_context`).
2. Context is assembled in a fixed order (`runtime/context_bundle.py`):
   preferences → **rules (injected every turn, always)** → project brief →
   skills → **workflows (mandatory procedures — injected before the repo map
   and never dropped, even in low-context "cave" mode)** → repo summary →
   AST repo map → memory recall.
3. A multi-part message ("fix X. also why Y? and add Z") gets a derived
   **checklist** pinned into the context; the agent flips items live via the
   `plan_progress` tool (`chat_agent/_loop.py`).
4. The model runs a ReAct loop speaking a **text protocol** — each turn is
   `THOUGHT:` + `ACTION: <tool>` + `ARGS_JSON: {...}`, or `FINAL: <answer>`.
   No native tool-calling, so it works on any backend (LM Studio, vLLM,
   OpenRouter, cloud).
5. Tools execute; risky ones pause for approval (gating: [TOOLS.md](TOOLS.md)).
6. On `FINAL` for a multi-part ask, a one-time **completeness gate** makes the
   model self-check its answer against the checklist.
7. **Auto-escalation:** a multi-file BUILD request in simple mode routes into
   the pipeline instead (`runtime/chat_router.py`). Plan mode is read-only and
   never escalates.

Answers **stream** to the UI as SSE, in simple and team mode alike
(`api/routes/chat.py`, `api/routes/_sse.py`).

### 2b. Pipeline (team mode / tickets)

Runs for `mode: team`, tickets, and escalated chat builds
(`runtime/pipeline.py` + `runtime/parallel_subtasks/`).

1. **Enhancer** rewrites the raw ask into a spec. A **degenerate-output guard**
   restores the raw ask if the rewrite collapsed or lost every named
   file/symbol (`pipeline._make_enhancer_guard`,
   `parallel_subtasks/_planning._spec_degenerate`).
2. **Architect** emits a file plan. A deterministic **plan gate** validates it
   (file dump, missing tests, mixed languages …) and gives the model exactly
   **one semantic reask**; a still-broken retry ships the sanitized plan
   (`_planning._validate_plan`).
3. If the plan has no test files, a **test backstop** adds a unit-test subtask
   per code module (`_stream._ensure_test_coverage`).
4. **SPEC.md is always written** to the workspace before any subtask runs — the
   shared contract every worker builds against. Mid-run steering appends to it.
5. Subtasks run **in parallel (default on, max 4 workers)**, each in its **own
   git worktree** with a fresh context: only its goal + the right SPEC.md slice.
   **Test subtasks are built first**; per-subtask validation is compile/build.
6. Successful branches **merge sequentially** into the ticket branch.
7. A fresh-context **spec verification** pass reads SPEC.md + the produced tree
   and confirms every requirement was addressed
   (`_reconcile/_integration._verify_against_spec`). Off-plan phantom files are
   pruned.
8. **Reconcile loop** compiles + tests the merged tree and fixes cross-file
   drift until green. Inside it: a **config-validity gate** (a broken
   pyproject/build file is fixed first — nothing runs until it parses,
   `_reconcile/_testrun._broken_project_config`), **escalation** of a stuck
   residual to a stronger model (`AIFORGE_ESCALATION_MODEL`), and a
   **test-audit** (a wrong test assertion may be corrected, marked with a
   `# test-audit:` comment).
9. A PR is opened (`runtime/git_pr/`) with an honest verdict (green / some
   tests fail / couldn't run here).

A subtask that fails big is **re-decomposed one level deeper**
(`AIFORGE_DECOMP_MAX_DEPTH`, default 2). Analysis asks fan out by repo instead
(`runtime/analysis_pipeline.py`).

---

## 3. Tool surface

One line per group. The complete per-tool reference (args, gating, which agent
gets what) is **[TOOLS.md](TOOLS.md)** — 112 tools in the chat registry.

| Group | What it gives you |
|---|---|
| Files / edit | read, batched `read_files`, write, patch, `editor` (str_replace/insert/undo, syntax-checked), atomic `multi_edit`, `rename_symbol` |
| Search / nav | ripgrep, find, AST `repo_map` (injected as context in chat), `lsp` (goto-def / refs / hover) |
| Code graph | `codegraph_query` / `_callers` / `_callees` / `_impact` / `_explore` — symbol relations from a pre-indexed SQLite graph, no file scanning |
| Code exec / tests | `run_command`, per-test `run_tests`, `typecheck`, `format`, persistent `ipython`, project detect+build/run, background `serve`, `ensure_runtime` (installs missing toolchain) |
| Git / PR | targeted git via shell, `github_pr`, `gitlab_mr_create` / comment |
| Jira / Confluence / GitLab | read + write ops, Agile boards, dashboards; GitLab CI pipelines incl. watch-to-completion |
| Email | `email_send` (approval-gated) / `email_read` |
| Web | `web_fetch`, `web_crawl` → markdown dossier in `work/web/`. NO web search — the query string was unfiltered outbound data (removed 2026-09-03) |
| Browser / UI | `browse` (Playwright), `ui_check` (screenshot + what a vision model SEES + console/network errors), `ui_ask` |
| Memory / learning | `memory_lookup`, `memory_write`, `remember_rule`, skill + workflow search/learn, `note_consolidate` / `note_curate` |
| Waiting / scheduling | `watch_until` (a whole poll loop for ONE model request), `schedule_task` (outlives the chat) |
| Sandbox | `mount_folder` / `unmount_folder` (request host access), `save_secret` |
| Resolvers | loose name → real thing: `resolve_repo`, `jira_resolve_project`, `confluence_resolve_space` |
| Scripts | `aiforge-tool <name> '<json>'` CLI — job/workflow scripts call the same registry (read-only by default; `runtime/tool_cli.py`) |

---

## 4. Memory & knowledge

- **Scoped OKR briefs** (`memory/md_store/`, `runtime/work_notes/`): the active
  memory is Markdown briefs under `~/.aiforge/memory/compacted/`, one per scope
  (global / per-repo / per-topic), each an OKR envelope (Objective / Key
  Results / Facts / Links / Learnings). Every write goes through `capture()`,
  tagged by **topic** and by the **agent** that wrote it. A **session execution
  ledger** (`runtime/session_ledger.py`) injects "already ran — don't repeat".
  Housekeeping is ONE evening pass. Full detail: **[OKR_MEMORY.md](OKR_MEMORY.md)**.
- **OKF on disk** (`memory/okf/`): every knowledge file is frontmatter + body,
  path is identity, links are edges — **[OKF.md](OKF.md)**. The typed node-DAG
  is dormant by default (`AIFORGE_OKR_DAG=0`).
- **Unified recall** (`memory/unified_query/`): one query fans out in parallel
  to all sources — SQLite vector/keyword search, ticket brief, code-symbol
  lookup, markdown/SOP docs — scored, weighted, deduped, top-K. **Code chunks
  are demoted** (`AIFORGE_UMEM_CHUNK_SCORE`, default 0.4) so curated knowledge
  outranks raw RAG.
- **Shared work folders** (`runtime/work_context.py`): work about a durable
  thing lives in `~/.aiforge/work/<kind>/<key>/` (`jira/PROJ-123`,
  `confluence/<page>`, `repo/<name>`, `web/`) — shared across every session that
  touches that context. Plain chats stay ephemeral.
- **Chunking** (`packages/aiforge_memory/`): code uses our **own AST chunker**
  over `tree_sitter_language_pack`; prose/docs use chonkie's text chunkers; any
  failure falls back to plain line windows — ingestion never breaks.
- **Graphify graph**: `graphify_lookup` reads a repo's `graphify-out/graph.json`
  directly — nodes, k-hop neighbours, typed relations incl. LLM-extracted
  `rationale_for` edges (`runtime/graphify_lookup_tool.py`). No database.
  Refresh: `scripts/runtime/aiforge-graphify-all.sh`; install via
  `run.sh --with-graphify`.
- **Langfuse mirror**: when enabled, every LLM call *and* every memory recall is
  mirrored to the local Langfuse **v2** UI (single container, no ClickHouse),
  fire-and-forget — with sessions (per chat) and a per-turn score
  (`integrations/langfuse_adapter.py`).

### Fleet memory sync (admin / spoke)

One box is the **admin**; every other box is a **spoke** that pushes its
knowledge there and pulls back the fold (`memory/sync/`). No mesh, no election,
no auth on the sync surface.

| Piece | Where |
|---|---|
| Role: admin unless `AIFORGE_ADMIN_URL` names one | `memory/sync/role.py` |
| Push / pull loop, 30 min default | `memory/sync/loop.py` (`DEFAULT_INTERVAL`) |
| Outbound filter — secrets, noise, private | `memory/sync/redact/` |
| Groups: one admin serves several fleets | `AIFORGE_SYNC_GROUP`, `run.sh --group` |
| Fold: peers' inbox + own tree → `mesh/`, read by spokes as their view | `memory/okf/tiers.py` |

`./run.sh --admin` claims the role, `--spoke` gives it up, `--admin-url <url>`
names the admin, `--admin-page` opens the loopback-only sync page.

---

## 5. Skills, workflows & rules

Three kinds of reusable instruction, all plain markdown with one unified
frontmatter (`name` / `description` / `triggers` / `scope`), managed in the
Library UI, authored in chat, or written by the agent itself.

| Kind | What | When it fires |
|---|---|---|
| Skill | know-how for a kind of task | relevance match on `triggers` + description |
| Workflow | end-to-end procedure (ordered steps, optional runnable `scripts/`) | relevance match; **mandatory** once matched — survives cave mode |
| Rule | always-on constraint | every turn (`alwaysApply`) or when edited files match its `globs` |

Load order (later wins on a name clash): **builtin**
(`runtime/builtin_playbooks/`) → global (`~/.aiforge/…`) → repo-local
(`<repo>/.aiforge/`, `.claude/`, `.openhands/`). A custom item always outranks a
shipped builtin. Workflow scripts pass a hard run-before-save test gate
(`runtime/workflows.py`).

**They capture themselves.** Rules from a correction (`runtime/rule_capture/`),
skills and workflows from a turn that actually did work
(`runtime/artifact_capture.py`) — one capped LLM call on the `learner` role,
and it **dedupes before it writes** against the same similarity the nightly
merge sweep uses (`runtime/artifact_merge.py`), so near-duplicates never land.
A merge archives members before removing them; bundled playbooks are excluded.
Off via `AIFORGE_ARTIFACT_CAPTURE=0`.

At the end of a simple-chat turn the agent also **predicts the next step** and
either does it (safe + reversible + confident) or offers it as a chip — the
act/offer decision is a table over blast radius, never the model's
(`runtime/next_step/`).

---

## 6. Where to change what

Single-source seams — change these in ONE place:

| Concern | The one module |
|---|---|
| Context assembly (rules/prefs/skills/workflows/memory/repo-map) | `runtime/context_bundle.py: build_bundle()` |
| "Which repo am I" (repo key) | `runtime/repo_ident.py: repo_name()` |
| Jira/Confluence/GitLab HTTP config | `runtime/tools/_http_integration.py: integration_conf()` |
| Outbound network for declared destinations | `net/egress.py` |
| Rules store | `runtime/repo_rules.py` |
| Background threads/processes | `runtime/background.py: spawn()` |
| Repo → local folder mapping | `config/repo_map.py` + `runtime/workspace.py` |

Intentionally split (guarded, not merged): the two agent engines
(`chat_agent` text protocol vs native ADK) and the two tool registries
(`chat_agent._registry.TOOLS` vs `runtime/doer_tools/`) — cross-surface drift
fails a parity test + a startup check (`runtime/tool_manifest.py`).

Adding a cross-surface tool → add to BOTH registries + `tool_manifest.CROSS_SURFACE`.
Adding a context source → `context_bundle.build_bundle()`, nowhere else.

The full decision history lives in **[DECISIONS.md](DECISIONS.md)**.

---

## 7. Operating it

```bash
./run.sh                 # the default: an Ubuntu 24.04 docker sandbox
./run.sh --isolated      # sandbox on an internal network, egress via proxy
./run.sh --repos DIR     # mount YOUR projects folder as the project root
./run.sh --mount DIR     # approve one more host folder (repeatable)
./run.sh --stop | --logs | --shell
./run.sh --dev           # uvicorn --reload | --port N | --host H
./run.sh --test          # probe the configured model endpoint, then exit
./run.sh --reset-config  # wipe the saved agent config (backed up)
./run.sh --with-langfuse # start the self-hosted trace UI (:3005)
./run.sh --migrate       # force a re-converge of a prior install
```

Also: `--admin` / `--spoke` / `--admin-url` / `--admin-page` / `--group`
(fleet memory sync), `--install-model2vec` (semantic memory),
`--with-graphify`, `--skip-web`, and the memory-maintenance flags `--dedupe`,
`--recompact-all`, `--migrate-okf`, `--purge-code` (each runs, then exits).
`--lite` / `--hybrid` / `--no-build` / `--docker` are legacy no-ops.

Storage is embedded SQLite + Markdown memory — no Postgres, no Neo4j.

**Config** is `aiforge.env` at the repo root: one fixed, committed file,
identical on every box, which run.sh reads and **never writes to**. Anything
per-box goes in the real environment, which **overrides** the file. `.env` is
not read. Per-box state lives in `~/.aiforge/` (`agent_config.json`,
`integrations.json`, `langfuse.env`, `mounts.list`, `security/`, `work/`).

### What the sandbox can see

The box has your uid + passwordless sudo and no workspace jail, but of *this*
machine it sees only `~/.aiforge`, plus `--repos` and any folder you approved.
It **installs the tools a task needs** — `ensure_runtime` maps a binary to its
apt/brew/apk package, and a missing Chromium build is fetched once on the first
browser launch (`runtime/tools/ensure_runtime.py`, `tools/browser.py`).

Adding a folder is **two-sided on purpose**: the chat's `mount_folder` (and
Settings) only *records a request* in `~/.aiforge/mounts.list` — which the box
itself can write — and the HOST grants it, with `--mount DIR` or a `y` at the
prompt on the next `./run.sh`. Approvals are kept where the box cannot reach
them (`~/.config/aiforge/approved-mounts`); home itself and `/` are refused
(`runtime/sandbox_mounts.py`, `run.sh`).

### `--isolated`

The box goes on an **internal** docker network with no route out
(`docker-compose.isolated.yml`, which REPLACES the default compose file rather
than overlaying it — `network_mode: host` cannot be taken back off).

| Container | Role |
|---|---|
| `aiforge` | on `box` only; `http_proxy`/`https_proxy` point at the proxy, set here as a boundary, not a preference |
| `egress` | the only thing on both networks — a proxy holding the allowlist generated from `AIFORGE_EGRESS_ALLOW_HOSTS` |
| `ui` | publishes the UI to the host's loopback (a container on an internal network cannot publish a port itself) |

The cost, stated plainly: the model endpoint is no longer at `127.0.0.1`. Point
`AIFORGE_LM_BASE_URL` at `http://host.docker.internal:1234/v1` and add that host
to the allowlist, or keep the model on the `out` side.

### Autonomous intake

`runtime/resolver.py` polls a GitHub repo for issues labelled `aiforge-bot` and
files each as a ticket (cron/systemd-timer friendly; `AIFORGE_RESOLVER_GH_REPO`,
`AIFORGE_RESOLVER_LABEL`, `GITHUB_TOKEN`).

### Env toggles

| Toggle | Default | Does |
|---|---|---|
| `AIFORGE_EGRESS_ALLOW_HOSTS` | unset | CSV allowlist for declared destinations; the proxy's list under `--isolated` |
| `AIFORGE_ALLOW_WEB_FETCH` | off | opens the web fetch/crawl egress gate (`AIFORGE_WEB_FETCH_DISABLE=1` is the hard-off) |
| `AIFORGE_UNATTENDED_WRITES` | off | lets a ticket/cron run write outward with no approver |
| `AIFORGE_ALLOW_DELETE` | off | skips the destructive-delete confirmation |
| `AIFORGE_TOOL_POLICY` | — | per-tool overrides, e.g. `run_command=ask` |
| `AIFORGE_PARALLEL_SUBTASKS` | on | `0` disables the parallel fan-out |
| `AIFORGE_PARALLEL_SUBTASKS_MAX` | 4 | concurrent subtask workers (clamped 1–8) |
| `AIFORGE_DECOMP_MAX_DEPTH` | 2 | how deep a failed subtask may re-decompose |
| `AIFORGE_ESCALATION_MODEL` | unset | stronger model for a stuck reconcile residual |
| `AIFORGE_CAVE_MODE` | 1 (on) | lean context; `0` opts out on a big-window model |
| `AIFORGE_WORKSPACE_DIR` | unset | clamps the chat file scope (native mode) |
| `AIFORGE_EXTRAS` | unset | optional extras: `structured,crawl,chunking,embed-static` |
| `LANGFUSE_HOST` / `_PUBLIC_KEY` / `_SECRET_KEY` | unset | enables trace mirroring (`AIFORGE_LANGFUSE_DISABLE=1` kills it) |
