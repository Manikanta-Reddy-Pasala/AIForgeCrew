# AIForgeCrew

Autonomous code-fix pipeline. Plain-language ticket → enriched intent → PR.
Plus a full-filesystem chat coding agent.

## Quickstart (deploy anywhere)

No Postgres, no Neo4j, no GPU — clone and run:

```bash
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew.git
cd AIForgeCrew
./run.sh
```

Open **http://127.0.0.1:8799/ui/**. The landing page is config-first: pick a
provider + model for each pipeline step. Choose **OpenAI-compatible** and paste any
base URL — LM Studio (`http://localhost:1234/v1`), OpenRouter, Groq, Together, vLLM,
or a cloud endpoint with a key. Leave the key blank for no-token OSS endpoints. Hit
**Test connection** to verify. Then use **Chat** (a full-filesystem coding agent) or
file a **Ticket** (the full pipeline).

Storage defaults to embedded **SQLite** (tickets) + a local **SQLite vector store**
(memory) under `~/.aiforge/`. To use the heavier "pro" backends, set
`AIFORGE_PG_URL` (Postgres tickets) and/or `NEO4J_URI` (graph memory) and they take
over automatically.

> ⚠️ **Security.** By default the Chat agent has **full, unsandboxed filesystem and
> shell access** on the host. Set `AIFORGE_WORKSPACE_DIR=/path/to/workspace` to clamp
> file/exec operations to one directory, and run shared/untrusted deployments inside a
> container. Treat the chat box like a terminal.

`./run.sh --dev` enables hot reload; `--port N` / `--host H` change the bind;
`--skip-web` skips the UI rebuild.

### Full "pro" stack with Docker Compose

For the heavier setup (Postgres + Neo4j graph memory + embedding sidecars),
`docker-compose.yml` runs everything as one stack:

```bash
docker compose up -d --build      # api+ui on :8799, postgres, neo4j, embed, rerank
```

## Features

- **Ticket → PR pipeline** — a plain-language ticket runs a multi-agent flow
  (triage → enhance → plan → verify → doer-loop → learn → validate) and opens a PR.
- **Chat coding agent** — a full-filesystem agent in three modes: **Simple** (one agent),
  **Plan** (read-only — proposes a plan before touching anything), **Team** (the full
  pipeline, ticketless). Same strong tool surface as the pipeline Doer (see
  **[Coding agent tools](#coding-agent-tools)**).
- **Structured editing** — a syntax-checked **editor** (str_replace / insert / **undo**),
  **multi_edit** (batch find/replace across many files, validated then all-or-nothing),
  **diff preview** before writes.
- **Code intelligence** — **LSP** go-to-def / find-refs / hover, **typecheck**, **format**,
  per-test **run_tests**, persistent **ipython** REPL, **glob** / **grep** / AST **repo-map**.
- **Context engineering** — auto **compaction** (rolling summary near the window limit),
  **dynamic per-turn context** (memory recall + `@-mentions` + repo-map injected every
  turn), and **Cave mode** (lean context for small local models). See
  **[Context engineering](#context-engineering)**.
- **Human-in-the-loop** — per-tool **allow/ask/deny** policy + a command **risk**
  classifier; risky/caution actions (sudo, force-push, chmod 777) **pause for
  Approve/Reject** with a diff preview, by default, in chat *and* the pipeline.
  Autonomous ticket runs never block.
- **VCS** — open **GitHub PRs** (`gh`) and **GitLab MRs** from the agent; targeted
  staging (blanket `git add -A` is refused).
- **Integrations** — **Jira** / **Confluence** / **GitLab** (search/read/create/update,
  incl. attached images + docs analysed as part of the task), **web search**
  (keyed Tavily/Brave with DuckDuckGo fallback) + **web fetch**, **browser** automation,
  **MCP** tools.
- **Attachments & vision** — paste/attach **images, PDFs, xlsx, docx**; stored per
  session, described + queryable all session; vision auto-detected or set per model.
- **Workspace checkpoints** — auto-snapshot before each turn; one-click **restore**.
- **Skills & workflows** — reusable `SKILL.md` / `WORKFLOW.md` playbooks, relevance-
  searched + auto-injected; the agent **authors new ones** when it solves something.
- **Memory** — frontier agent-memory that learns across runs (see **[Memory](#memory)**).
- **Resilient streaming** — navigate away and back without losing a running turn;
  cancel/abort mid-generation; a **kill-all** to clear a wedged run.
- **Providers** — local (LM Studio / mlx-lm) or any OpenAI-compatible endpoint, with
  automatic cloud escalation; a **model registry** (add a model once, every agent picks
  it by name) with per-model vision + context window.

## Agents

A ticket (or Team chat) flows through specialized agents — each on the model you pick,
with automatic cloud fail-over. Pick any provider per role.

**Pipeline (ticket → PR)**
- **Triage** — sizes the work; routes trivial tasks straight to the Doer.
- **Enhancer** — fixes the prompt + folds in memory/history/repo context → a clean spec.
- **Architect** — designs the file structure (disjoint responsibilities).
- **Planner** — breaks the spec into scoped subtasks.
- **Verifier** — one multi-axis plan critic (correctness · scope · risk).
- **Doer ↔ Refiner ↔ Feedback** — the build loop: edits in an isolated git worktree,
  runs build/tests, self-corrects until the change holds.
- **Validator** — final pre-PR gate incl. a **test-depth** check (rejects happy-path-only tests).
- **Live-verifier** — runs the project's real recipe to confirm it *works*, not just compiles.
- **Learner** — writes back facts/decisions/skills; auto-mines the run for durable memory.

**Context gatherers (parallel)** — **Researcher**, **Repo-map** (AST PageRank), **Conventions**.

**Chat** — full-filesystem coding agent in **Simple** / **Plan** (read-only) / **Team** modes.

## Coding agent tools

The chat agent and the pipeline Doer share one tool surface (the chat agent speaks a
plain-text ReAct protocol so it works on **any** OpenAI-compatible backend — no native
tool-calling required):

| Group | Tools |
|-------|-------|
| **Files** | `file_read` · `file_write`/`file_create` · `file_patch` · **`editor`** (view/create/str_replace/insert/**undo**, syntax-checked) · **`multi_edit`** (batch, many files, atomic) · `list_dir` |
| **Search / nav** | `grep` (ripgrep) · `find` · **`glob`** · AST **`repo_map`** · **`lsp`** (goto-def / find-refs / hover) |
| **Code** | `run_command` · **`run_tests`** (per-test) · **`typecheck`** · **`format`** · **`ipython`** (persistent REPL) · `project` (detect+build/test/run) · `serve` (background dev server) · `ensure_runtime` |
| **VCS** | targeted `git` (via shell) · **`github_pr`** · **`gitlab_mr_create`** / `gitlab_mr_comment` |
| **Integrations** | `jira_*` · `confluence_*` · `gitlab_*` · `web_search` · `web_fetch` · `browser` · `mcp` |
| **Memory / learning** | `memory_lookup` · `memory_write` · `remember_rule` · `skill_search` / `learn_skill` · `workflow_search` / `learn_workflow` |

Writes show a **diff**; risky/caution commands and external writes are **approval-gated**
by default. An optional `AIFORGE_WORKSPACE_DIR` clamps file ops to a root for cautious
deploys.

## Context engineering

How the agent stays coherent over long sessions on a finite window:

- **Compaction** — when the running history approaches the model's context window
  (`_ctx_budget_chars`, sized from the per-model window), older turns auto-collapse into
  a **rolling summary** breadcrumb (earlier asks + outcomes + tools used) while the recent
  tail stays verbatim. Heuristic — no extra LLM call — so it runs every turn for free.
- **Dynamic context** — before **every** turn the agent injects fresh **memory recall**
  (RAG over the knowledge graph, keyed to the current message), `@-mentions`, the
  **repo-map**, the project summary, session files, and the user's rule book — so
  follow-ups and post-compaction turns don't "forget".
- **Cave mode** — a one-click lean-context toggle (chat top-bar / Settings): smaller
  repo-map, skips the optional skills/workflows/mention blocks, fewer recall hits,
  condenses sooner. Cheaper + faster on a small local model. Global, also applies to the
  team pipeline.

## UI

- **Chat** — the coding agent: mode toggle, model picker, 🦴 Cave toggle, attachments
  (image/pdf/xlsx/docx + paste), live steps, diff approvals, checkpoints, a `⋯` menu.
- **Tickets / Board / Dashboard** — the ticket → PR pipeline and its runs.
- **Workflow** — a live diagram of the pipeline graph: Triage → Orchestrator
  (Enhancer → Planner) → parallel Context fan-out → Verify → Build loop → Validate →
  Learn. Nodes colour-coded by type; hover for what each does.
- **Agents** — every agent grouped (Orchestrator / Pipeline / Fan-out & helpers / Chat)
  with a one-line description, its model, and live load.
- **Settings** — **Agent** tab (a model registry — add a model once, point any agent at
  it; per-model vision + context window) and **Integrations** tab (Jira / Confluence /
  Git) + the LLM settings (max output, context window, vision, Cave mode).
- **Memory / Skills / Workflows / Rules** — browse + author what the system has learned.
- **Perf** — where time goes (LLM vs shell vs file I/O, live). **Logs** — per-agent live
  stream.

## Memory

Frontier agent-memory — on par with Mem0 / Zep / Letta and **ahead** on code intelligence.

- **Hybrid retrieval** — vector + full-text + graph fused (RRF + cross-encoder rerank),
  diversified, role-scoped.
- **Auto write-path** — every passing run is *mined*: extract durable facts → decide
  **ADD / UPDATE / DELETE / NOOP** vs existing (dedup + contradiction resolution) → write
  a reflection. No manual fact-logging.
- **Bi-temporal** — facts carry `valid_at`/`invalid_at`; corrections **supersede**
  non-destructively (history kept for audit, dropped from recall).
- **Importance + decay** — salience-weighted ranking, recency decay, **sleep-time
  consolidation** (`aiforge-memory maintain`).
- **Self-editing blocks** — the agent keeps its own persistent working-notes block per repo.
- **User preferences** — durable, cross-repo prefs injected so "always do X" sticks.
- **Code intelligence** *(ahead of the field)* — AST symbols, call-graph, LSP, repo-map,
  domains/flows/guided tours, cross-repo links, incremental delta-indexing.
- **Procedural** — auto-authored **Skills** + pattern-mining of repeated wins.
- **Tiers** — T1 episodic · T2 semantic · T3 procedural · T4 code; embedded SQLite by
  default, Postgres + Neo4j for the pro stack.

## How it works & improves

A ticket (or chat) flows through specialized agents, each on the model you pick; a
local primary auto-falls-over to a cloud endpoint if it stalls. The Doer edits in an
isolated git worktree, runs build/tests, and a second-agent review + verifiers gate
the PR. **It gets better over time:** every solved task writes facts, decisions, and
reusable skills into memory, and the next relevant ticket/turn recalls and re-injects
them — so the system stops re-deriving what it already learned.

## Configuration

Everything is configurable from the UI; env vars override at read time.

```
# Providers / models (per role: architect, planner, verifier, doer, feedback,
# learner, triage, researcher, refiner, chat, …)
AIFORGE_<ROLE>_PROVIDER        local | ollama_cloud | openai_compatible
AIFORGE_<ROLE>_MODEL           model id for that role
AIFORGE_<ROLE>_BASE_URL        endpoint (OpenAI-compatible)
OLLAMA_CLOUD_API_KEY           key for the cloud escalation target

# Chat controls
AIFORGE_WORKSPACE_DIR          clamp file/exec to one dir (security)
AIFORGE_TOOL_POLICY            e.g. "run_command=ask,file_write=deny"
AIFORGE_RISK_ASK_CAUTION       gate caution cmds (sudo/chmod 777/force-push); default ON, =0 to opt out
AIFORGE_CAVE_MODE              1 = lean context (smaller repo-map, skip optional blocks, condense sooner)
AIFORGE_CHAT_AUTO_CHECKPOINT   1 = snapshot before each turn (default)
AIFORGE_CHAT_AUTO_MEMORY       1 = persist a memory note per turn (default)
AIFORGE_SKILLS_DIR             skill registry root (default ~/.aiforge/skills)

# Web search (optional keyed providers; falls back to keyless DuckDuckGo)
AIFORGE_TAVILY_API_KEY         Tavily search key (preferred when set)
AIFORGE_BRAVE_API_KEY          Brave Search key (fallback)

# Storage (optional "pro" backends; embedded SQLite by default)
AIFORGE_PG_URL                 Postgres tickets
NEO4J_URI                      graph memory

# Confluence (chat tools: search/read/create/update pages; Server/Data Center)
CONFLUENCE_BASE_URL            e.g. https://confluence.internal
CONFLUENCE_TOKEN               Personal Access Token (Bearer)
CONFLUENCE_USER                set ⇒ Basic auth (user + token) instead of Bearer
CONFLUENCE_INSECURE_TLS=1      skip TLS verify for a self-signed internal cert
```

Confluence writes (`confluence_create`/`confluence_update`) go through the chat
**approval gate** by default — the agent proposes, you Approve/Reject.

## Project layout

```
aiforge_core/
  agents/         archetypes (architect, planner, verifier, doer, feedback,
                  learner, …) + agents.yaml (per-role tools / scopes / contracts)
  runtime/        the ADK pipeline, chat agent, tools, memory wiring, guards
  api/            FastAPI app + UI (port 8799)
  memory/         unified_query (vector + full-text + graph) + decay + store
  config/         providers (agent_config) + env + roles
  llm/            litellm client + router
  tickets/        ticket lifecycle (SQLite or Postgres)
web/              React UI
```

## Security rails

- **Workspace clamp** — `AIFORGE_WORKSPACE_DIR` confines file/exec to one dir.
- **Approval gate** — risky / `ask`-policy actions pause for Approve/Reject.
- **Delete guard** — destructive deletes refused unless explicitly confirmed.
- **Scope guard** — Doer edits blocked outside the ticket's `scope_allowlist_globs`.
- **Plan mode** — read-only; proposes a plan before any write.
