# AIForgeCrew

Autonomous code-fix pipeline. Plain-language ticket → enriched intent → PR.
Plus a full-filesystem chat coding agent.

> ### 👉 Read this first: **[QUICKSTART.md](QUICKSTART.md)**
> The complete getting-started guide — **run it**, **configure models**,
> **configure integrations**, and **create jobs / rules / skills / workflows**.

## Docs

| Doc | What's in it |
|---|---|
| **[SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md)** | How it works: request flow (chat + pipeline), memory, skills/workflows/rules, operating it |
| **[OKR_MEMORY.md](docs/OKR_MEMORY.md)** | The OKR-DAG memory — markdown nodes (objectives/key-results/learnings/sessions), typed edges, in-memory graph, surgical retrieval |
| **[TOOLS.md](docs/TOOLS.md)** | The complete tool reference — every tool, args, gating, per-agent allowlists |
| **[DECISIONS.md](docs/DECISIONS.md)** | Why things are the way they are (ADR-lite log, evidence-linked) |
| **[DEMO_GUIDE.md](docs/DEMO_GUIDE.md)** | A guided demo walkthrough |

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
or a cloud endpoint with a key. Hit **Test connection** to verify. Then use **Chat**
(a full-filesystem coding agent) or file a **Ticket** (the full pipeline).

Storage defaults to embedded **SQLite** under `~/.aiforge/`. Set `AIFORGE_PG_URL`
(Postgres tickets) and/or `NEO4J_URI` (graph memory) and the "pro" backends take
over automatically — or run the whole pro stack with `docker compose up -d --build`.

> ⚠️ **Security.** By default the Chat agent has **full, unsandboxed filesystem and
> shell access** on the host. Set `AIFORGE_WORKSPACE_DIR=/path/to/workspace` to clamp
> file/exec operations to one directory, and run shared/untrusted deployments inside a
> container. Treat the chat box like a terminal.

`./run.sh --dev` enables hot reload; `--port N` / `--host H` change the bind.
Mode is `AIFORGE_MODE`-driven (`lite` | `hybrid` | `docker`; a flag still wins):
`--lite` runs **zero-Docker** (host + SQLite for everything). `--migrate` moves an
existing Postgres (chat + tickets) into the SQLite stores and removes the DB infra
containers. `--with-langfuse` / `--stop-langfuse` manage the optional self-hosted
LLM trace UI — allowed even in `--lite`, so tracing can be the only container.

## Features

- **Ticket → PR pipeline** — a plain-language ticket runs a multi-agent flow
  (triage → enhance → plan → verify → doer-loop → learn → validate) and opens a PR.
  Multi-file builds decompose into **parallel per-subtask runs** (default on, up to 4),
  each in its own fresh context + git worktree, all building against a shared
  **SPEC.md** written up front.
- **Chat coding agent** — three modes: **Simple** (one agent; a multi-file *build*
  auto-routes through the pipeline; multi-part asks get a live checklist), **Plan**
  (read-only), **Team** (the full pipeline, ticketless). Speaks a plain-text ReAct
  protocol, so it works on **any** OpenAI-compatible backend — no native tool-calling.
- **Strong tool surface** — syntax-checked editor with undo, atomic multi-file edits,
  LSP/typecheck/format/per-test runs, persistent ipython, background dev servers,
  GitHub PRs + GitLab MRs. Full reference: **[docs/TOOLS.md](docs/TOOLS.md)**.
- **Integrations** — a broad **Jira** suite (issues, transitions, **time tracking**,
  **boards/sprints**, dashboards), **Confluence**, **GitLab**, **email**, **web
  search/fetch/crawl**, **browser** automation, **MCP**. `context_gather` builds a
  cached cross-entity **dossier** (ticket + linked pages + images, fetched in
  parallel). Every configured integration is also callable from shell scripts via
  the **`aiforge-tool`** CLI (read-only by default).
- **Context workspaces** — work about a durable thing gets a persistent folder
  shared across sessions: `~/.aiforge/work/{jira,confluence,repo,web}/<key>/`.
- **Skills, workflows & rules** — reusable playbooks + always-on rules, unified
  frontmatter, relevance-matched and auto-injected. Rules and matched workflows are
  **mandatory** (workflows survive Cave mode); workflow scripts pass a hard
  run-before-save test gate. The agent authors new ones when it solves something.
- **Human-in-the-loop** — per-tool **allow/ask/deny** policy + a command risk
  classifier; risky actions and external writes pause for **Approve/Reject** with a
  diff preview. Autonomous ticket runs never block.
- **Memory** — an **OKR-DAG**: markdown nodes (objectives → key results → learnings
  → sessions) with typed frontmatter edges build an **in-memory graph** (no DB), and
  *surgical* retrieval feeds the active goal's why/what/rules/recent into the prompt.
  Sessions **auto-author** durable Objectives/KRs/Learnings; the OKR envelope is
  topic-organized, tagged (by topic **and** which agent wrote it), split-on-oversize
  with cross-links, and compacted hourly. Plus a session **execution ledger** ("don't
  redo what already ran") and auto-captured **working workflows**. Vector+text+graph
  recall (SQLite embedded, or optional Neo4j) sits underneath, with code chunks
  demoted. Details: **[OKR_MEMORY.md](docs/OKR_MEMORY.md)** ·
  **[SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md#4-memory--knowledge)**.
- **Context engineering** — auto-compaction near the window limit, fresh per-turn
  memory recall + repo-map injection, and **Cave mode** (lean context for small
  local models).
- **Observability** — optional self-hosted **Langfuse v2** trace mirror (SDK-free REST,
  single lightweight container — no ClickHouse): every LLM call + memory recall/write,
  with sessions + a per-turn score. `./run.sh --with-langfuse` hosts it (UI :3005, keys
  auto-generated, 1-day retention) — works in `--lite` too. Plus on-disk traces under
  `~/.aiforge/` regardless.
- **Providers** — local (LM Studio / mlx-lm) or any OpenAI-compatible endpoint, with
  automatic cloud escalation; a model registry with per-model vision + context window.
- **Resilient streaming** — navigate away and back mid-turn; cancel/abort; checkpoints
  with one-click restore; attachments (image/pdf/xlsx/docx) with vision captioning.

## Agents

A ticket (or Team chat) flows through specialized agents — each on the model you
pick, with automatic cloud fail-over:

- **Triage** → **Enhancer** → **Architect** → **Planner** → **Verifier** (multi-axis
  plan critic) → **Doer ↔ Refiner ↔ Feedback** (build loop in an isolated worktree)
  → **Validator** → **Live-verifier** (runs the real recipe) → **Learner** (writes
  memory back).
- **Context gatherers** run in parallel: Researcher, Repo-map (AST PageRank),
  Conventions.
- Per-agent tool allowlists (and which stages are tool-less) are in
  **[docs/TOOLS.md](docs/TOOLS.md#which-agent-gets-which-tools)**.

## Coding agent tools (summary)

| Group | Tools |
|-------|-------|
| **Files** | `file_read` · `file_write` · `file_patch` · **`editor`** (syntax-checked, undo) · **`multi_edit`** (atomic batch) · `list_dir` |
| **Search / code** | `grep` · `find` · **`lsp`** · `run_command` · **`run_tests`** · `typecheck` · `format` · `ipython` · `project` · `serve` |
| **VCS** | targeted `git` · **`github_pr`** · GitLab MRs |
| **Integrations** | `jira_*` (21) · `confluence_*` (14) · `gitlab_*` · email · `web_search`/`web_fetch`/`web_crawl` · `browser` · `mcp` · **`context_gather`** · resolvers |
| **Memory / learning** | `memory_lookup` / `memory_write` · `remember_rule` · skill + workflow search/learn |
| **Progress** | **`plan_progress`** (live checklist for multi-part asks) |

Full reference — every tool with args, gating (read-only / approval-gated /
plan-mode), and per-agent access: **[docs/TOOLS.md](docs/TOOLS.md)**.

Writes show a **diff**; risky commands and external writes are **approval-gated** by
default; blanket `git add -A` is refused; destructive deletes need confirmation.

## Configuration

Everything is configurable from the UI; env vars override at read time. Full
annotated list: **[.env.example](.env.example)**. Quick hits:

```
AIFORGE_<ROLE>_PROVIDER / _MODEL / _BASE_URL   per-role model routing
AIFORGE_WORKSPACE_DIR          clamp file/exec to one dir (security)
AIFORGE_TOOL_POLICY            e.g. "run_command=ask,file_write=deny"
AIFORGE_CAVE_MODE              1 = lean context for small local models
AIFORGE_PARALLEL_SUBTASKS(_MAX)  pipeline fan-out (default on, 4)
AIFORGE_LANGFUSE=1             self-host the trace UI (or LANGFUSE_HOST + keys)
AIFORGE_PG_URL / NEO4J_URI     optional "pro" storage backends
JIRA_BASE_URL / JIRA_TOKEN     Jira (also in UI → Settings → Integrations)
CONFLUENCE_BASE_URL / _TOKEN   Confluence (same pattern; _USER ⇒ Basic auth)
```

Jira/Confluence **writes** go through the approval gate; reads never prompt.
Unconfigured tools degrade cleanly (`*_not_configured` + a hint), never crash.

## Project layout

```
aiforge_core/
  agents/         archetypes + agents.yaml (per-role tools / scopes / contracts)
  runtime/        the ADK pipeline, chat agent, tools, memory wiring, guards
  api/            FastAPI app + UI (port 8799)
  memory/         unified_query (vector + full-text + graph) + decay + store
  integrations/   optional lib adapters (instructor, crawl4ai, chonkie,
                  langfuse, ragas) — thin seams, graceful without the lib
  config/         providers (agent_config) + env + roles
packages/aiforge_memory/   standalone memory package (chunking, embeddings)
web/              React UI
```

## Security rails

- **Workspace clamp** — `AIFORGE_WORKSPACE_DIR` confines file/exec to one dir.
- **Approval gate** — risky / `ask`-policy actions pause for Approve/Reject.
- **Delete guard** — destructive deletes refused unless explicitly confirmed.
- **Scope guard** — Doer edits blocked outside the ticket's `scope_allowlist_globs`.
- **Plan mode** — read-only; proposes a plan before any write.
