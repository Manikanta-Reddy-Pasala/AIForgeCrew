# AIForgeCrew

Autonomous code-fix pipeline. Plain-language ticket → enriched intent → PR.
Plus a full-filesystem chat coding agent.

> ### 👉 Read this first: **[QUICKSTART.md](QUICKSTART.md)**
> The complete getting-started guide — **run it**, **configure models**,
> **configure integrations**, and **create jobs / rules / skills / workflows**,
> plus indexing code into memory and using chat & tickets.

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
`--skip-web` skips the UI rebuild; `--with-langfuse` / `--stop-langfuse` manage the
optional self-hosted LLM trace UI (see Features).

### Full "pro" stack with Docker Compose

For the heavier setup (Postgres + Neo4j graph memory + embedding sidecars),
`docker-compose.yml` runs everything as one stack:

```bash
docker compose up -d --build      # api+ui on :8799, postgres, neo4j, embed, rerank
```

## Features

- **Ticket → PR pipeline** — a plain-language ticket runs a multi-agent flow
  (triage → enhance → plan → verify → doer-loop → learn → validate) and opens a PR.
  Multi-file builds **decompose into per-subtask runs** — each in its own fresh
  context + git worktree (default **on**, up to 4 concurrent), all building against a
  shared **SPEC.md** written up front.
- **Chat coding agent** — a full-filesystem agent in three modes: **Simple** (one agent —
  a multi-file *build* auto-routes through the build pipeline; multi-part asks show a
  live checklist the agent ticks via `plan_progress`), **Plan** (read-only — proposes a
  plan before touching anything), **Team** (the full pipeline, ticketless). Same strong
  tool surface as the pipeline Doer (see **[Coding agent tools](#coding-agent-tools)**).
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
- **Integrations** — a broad **Jira** suite (search/read/create/update/comment,
  transitions, assign, links, **time tracking** — estimates + worklogs + `log_work`,
  **boards/sprints**, projects, **dashboards**) and **Confluence** (search/read/create/
  update, spaces, page-by-title, labels, comments, descendants) for **Server/Data
  Center**; **GitLab**; **email** (SMTP send / IMAP read); **web search** (keyed
  Tavily/Brave with DuckDuckGo fallback) + **web fetch** + **web crawl** (page →
  saved markdown dossier); **browser** automation; **MCP** tools. Every configured
  integration tool is also callable from shell scripts via the **`aiforge-tool`** CLI
  (read-only tools by default) — job/workflow scripts use it instead of raw `curl`.
- **Context dossier** — ask to explain a Jira ticket (or Confluence page) and
  `context_gather` pulls the entity **plus its linked pages / tickets / images in
  parallel**, saves each into the context folder, merges a `dossier.md`, and caches it —
  re-asking is instant and refreshes only when the entity changed.
- **Context workspaces** — a chat about a durable thing gets a **persistent folder
  shared across sessions**: `~/.aiforge/work/jira/<KEY>/`, `…/confluence/<page-id>/`,
  `…/repo/<name>/`, `…/web/<slug>/` (crawled pages). A ticket's images, its Confluence
  pages, and scratch all live inside its folder; a plain chat stays an ephemeral
  per-session scratch dir.
- **Attachments & vision** — paste/attach **images, PDFs, xlsx, docx**; docs' text is
  extracted and images captioned by a vision model (route a `vision` agent at a VLM when
  the chat model is text-only). Ticket/page attachments persist in the context folder.
- **Workspace checkpoints** — auto-snapshot before each turn; one-click **restore**.
- **Skills, workflows & rules** — reusable playbooks + always-on rules with a **unified
  frontmatter** (name / description / triggers / scope), relevance-matched (fuzzy trigger
  matching) + auto-injected; a matching skill/workflow's **output format is reproduced
  exactly** (verbatim, even after a tool call). A workflow can carry **runnable scripts**
  (`<name>/scripts/`) — each script's declared test command is **actually run before the
  workflow saves** (a failing script is never saved). Rules inject as a **MANDATORY**
  block; a matched workflow is mandatory too and survives Cave mode. Build them from chat
  (New skill/rule/workflow) or the Library; the agent also **authors new ones** when it
  solves something.
- **Memory** — frontier agent-memory that learns across runs (see **[Memory](#memory)**).
- **Resilient streaming** — navigate away and back without losing a running turn;
  cancel/abort mid-generation; a **kill-all** to clear a wedged run.
- **Observability** — optional self-hosted **Langfuse** trace mirror (SDK-free REST,
  async): **every** LLM call across chat + pipeline roles, plus memory recall/write.
  `./run.sh --with-langfuse` hosts the stack for you (UI on :3005, keys auto-generated,
  1-day retention prune) — or point `LANGFUSE_HOST` + keys at an existing server.
  AIForge's own file tracing stays the source of truth; unset = zero overhead.
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
| **Integrations** | `jira_*` (read/create/update/comment · transitions · worklog / `log_work` · boards / sprints / projects · dashboards · remote_links) · `confluence_*` (read/create/update · spaces · page_by_title · labels · comments · descendants) · **`context_gather`** (parallel cross-entity dossier) · **resolvers** (`resolve_repo` · `jira_resolve_project` · `confluence_resolve_space` — loose name → real path/key) · `gitlab_*` · `email_send` / `email_read` · `web_search` · `web_fetch` · **`web_crawl`** (page → markdown dossier in `work/web/`, crawl4ai when installed) · `browser` · `mcp` |
| **Memory / learning** | `memory_lookup` · `memory_write` (per-context or **`scope:"global"`**) · `remember_rule` · `skill_search` / `learn_skill` · `workflow_search` / `learn_workflow` |
| **Progress** | **`plan_progress`** (multi-part asks in Simple mode get a live checklist; the agent flips items running → done) |

Writes show a **diff**; risky/caution commands and external writes are **approval-gated**
by default. An optional `AIFORGE_WORKSPACE_DIR` clamps file ops to a root for cautious
deploys. Shell scripts (jobs, workflow scripts) call the same configured integration
tools through the **`aiforge-tool`** console script (`aiforge-tool jira_search '{…}'`,
`--list` to enumerate) — read-only tools only, by default.

**Optional integration adapters** (`aiforge_core/integrations/` — separation of concerns:
libs are imported ONLY behind thin adapters; every seam degrades gracefully without them;
`./run.sh` installs the first three automatically via `.[structured,crawl,chunking]`):
[instructor](https://github.com/567-labs/instructor) — Pydantic-validated LLM output with
auto-reask at the architect/grader/steering seams; a built-in schema-prompt+reask fallback
always works. [crawl4ai](https://github.com/unclecode/crawl4ai) — headless-browser
markdown for `web_crawl`; plain fetch fallback. [chonkie](https://github.com/chonkie-inc/chonkie)
— structure-aware **doc/markdown** chunking + boundary-respecting truncation of LLM-bound
files (code is chunked by our **own AST chunker** over `tree-sitter-language-pack`;
line-window fallback). [langfuse](https://github.com/langfuse/langfuse) — SDK-free REST
trace mirror (see Observability above). [ragas](https://github.com/explodinggradients/ragas)
RAG scoring and [dspy](https://github.com/stanfordnlp/dspy) prompt experiments stay
dev-tool overlays, never app dependencies (`uv run --with 'ragas<0.4'
--with 'langchain-openai<1' python scripts/rag_eval.py`; `scripts/dspy_experiment.py`).

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
  repo-map, skips the optional skills/mention blocks, fewer recall hits, condenses
  sooner. Matched **workflows still inject** — they're mandatory user procedure, cave
  or not. Cheaper + faster on a small local model. Global, also applies to the team
  pipeline.

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
- **Scoped + global** — memory is keyed to its context (a repo, or a Jira ticket /
  Confluence page whose folder is the cwd) and **recall unions the scoped key with
  global** — so a ticket chat sees both that ticket's own facts and cross-ticket
  knowledge. `memory_write scope:"global"` writes a lesson recalled everywhere.
- **Compaction** — every write folds into a compacted per-scope brief
  (`compacted-<key>.md`) in one common path, re-summarised on a schedule to bound size;
  the per-scope brief + the global brief are injected into each chat. Md files
  (`~/.aiforge/memory`) and Neo4j stay in sync.
- **User preferences** — durable, cross-repo prefs injected so "always do X" sticks.
- **Code intelligence** *(ahead of the field)* — AST symbols, call-graph, LSP, repo-map,
  domains/flows/guided tours, cross-repo links, incremental delta-indexing.
- **Chunking** — code is chunked by our own **AST chunker** (tree-sitter, structure-first
  packing to a token budget), docs by chonkie's recursive splitter, with a line-window
  fallback (`AIFORGE_CODEMEM_CHUNKER` to pin a backend).
- **Traceable** — with Langfuse enabled, every `memory.recall` and `memory.write` is
  mirrored as a browsable trace next to the LLM calls.
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
AIFORGE_CTX_HISTORY_FRACTION   share of the (post-reserve) window kept as live chat
#                                history before condensing (=0.85; raise toward 0.95 to
#                                use more of a big model window, lower for more headroom)
AIFORGE_CHAT_AUTO_CHECKPOINT   1 = snapshot before each turn (default)
AIFORGE_CHAT_AUTO_MEMORY       1 = persist a memory note per turn (default)
AIFORGE_SKILLS_DIR             skill registry root (default ~/.aiforge/skills)

# Pipeline fan-out (per-subtask fresh context + git worktree)
AIFORGE_PARALLEL_SUBTASKS      1 = decompose multi-file builds into per-subtask runs (default ON)
AIFORGE_PARALLEL_SUBTASKS_MAX  concurrent subtasks (=4; set 1 on a strictly serial endpoint)
AIFORGE_AUTO_ESCALATE          1 = Simple mode routes a multi-file build through the pipeline (default ON)

# Reliability / timeouts (a slow local reasoning model)
AIFORGE_LLM_TIMEOUT_S          app→model read timeout (=900s / 15 min)
AIFORGE_CHAT_LLM_RETRIES       transient-failure retries in chat before surfacing (=5)

# Langfuse tracing (optional; see Observability)
AIFORGE_LANGFUSE=1             run.sh hosts the trace stack (UI :3005, keys auto-generated)
LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY   point at an existing server instead

# Web search (optional keyed providers; falls back to keyless DuckDuckGo)
AIFORGE_TAVILY_API_KEY         Tavily search key (preferred when set)
AIFORGE_BRAVE_API_KEY          Brave Search key (fallback)

# Storage (optional "pro" backends; embedded SQLite by default)
AIFORGE_PG_URL                 Postgres tickets
NEO4J_URI                      graph memory

# Jira (issues, time tracking, boards/sprints, dashboards; Server/Data Center)
JIRA_BASE_URL                  e.g. https://jira.internal
JIRA_TOKEN                     Personal Access Token (Bearer)
JIRA_USER                      set ⇒ Basic auth (user + token) instead of Bearer
JIRA_INSECURE_TLS=1            skip TLS verify for a self-signed internal cert
# (all four also settable in the UI → Settings → Integrations)

# Confluence (search/read/create/update, spaces, labels, comments; Server/Data Center)
CONFLUENCE_BASE_URL            e.g. https://confluence.internal
CONFLUENCE_TOKEN               Personal Access Token (Bearer)
CONFLUENCE_USER                set ⇒ Basic auth (user + token) instead of Bearer
CONFLUENCE_INSECURE_TLS=1      skip TLS verify for a self-signed internal cert
```

Jira/Confluence **writes** (create/update/comment/`log_work`, dashboard create) go
through the chat **approval gate** — the agent proposes, you Approve/Reject; reads never
prompt. On Jira **Data Center**, dashboard *reads* work but *create* has no REST endpoint
(the tool returns a "create it in the UI" hint). Unconfigured tools degrade cleanly
(`*_not_configured` with the env to set), never crash.

## Project layout

```
aiforge_core/
  agents/         archetypes (architect, planner, verifier, doer, feedback,
                  learner, …) + agents.yaml (per-role tools / scopes / contracts)
  runtime/        the ADK pipeline, chat agent, tools, memory wiring, guards
  api/            FastAPI app + UI (port 8799)
  memory/         unified_query (vector + full-text + graph) + decay + store
  integrations/   optional lib adapters (instructor, crawl4ai, chonkie,
                  langfuse, ragas) — thin seams, graceful without the lib
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
