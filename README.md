# AIForgeCrew

Autonomous code-fix pipeline — plain-language ticket → enriched intent → PR —
plus a full-filesystem chat coding agent.

```bash
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew.git
cd AIForgeCrew
./run.sh
```

Then open **http://127.0.0.1:8799/ui/**. The landing page is config-first: pick a
provider + model per pipeline step, **Test connection**, then use **Chat** or file a
**Ticket**. Choose **OpenAI-compatible** and paste any base URL — LM Studio
(`http://localhost:1234/v1`), OpenRouter, Groq, Together, vLLM, or a cloud endpoint.
Needs **Docker**. No Postgres, no Neo4j, no GPU.

## Docs

| Doc | What's in it |
|---|---|
| **[QUICKSTART.md](QUICKSTART.md)** | Run it, configure models and integrations, create jobs / rules / skills / workflows |
| **[INSTALL.md](INSTALL.md)** | Docker vs native, how the sandbox works, credentials, offline and corporate-CA notes |
| **[SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md)** | Request flow (chat + pipeline), memory, skills/workflows/rules, operating it |
| **[TOOLS.md](docs/TOOLS.md)** | Every tool, args, gating, per-agent allowlists |
| **[OKR_MEMORY.md](docs/OKR_MEMORY.md)** | The OKR-DAG memory — markdown nodes, typed edges, surgical retrieval |
| **[OKF.md](docs/OKF.md)** | The on-disk memory format (Open Knowledge Format v0.1) |
| **[DECISIONS.md](docs/DECISIONS.md)** | Why things are the way they are (ADR-lite, evidence-linked) |

## How it runs

`./run.sh` starts AIForge in an **Ubuntu 24.04 sandbox**: full rights inside it, sees
only `~/.aiforge` of your machine, outbound network open. That is the only mode:
there is no host option to pick.

The first run installs what the project declares **from its lockfiles only** (`uv.lock`,
`web/package-lock.json`, `scripts/codegraph/package-lock.json`), builds the UI and starts;
later runs install nothing unless a lock changed. Nothing fetches a source and executes
it — `uv` and Node are ordinary wheels, npm runs no install scripts.

Storage is embedded **SQLite** (tickets + chat) + scoped **Markdown** memory under `~/.aiforge/`.

Settings live in **`aiforge.env`** — one committed file, identical on every box, which
run.sh reads and never writes. Per-box values (model endpoint, memory role, API keys) go
in the real environment, which **overrides** the file. `.env` is not read.

### Isolated network

`./run.sh --isolated` puts the box on an **internal** docker network with no route out.
All egress goes through a default-deny proxy holding `AIFORGE_EGRESS_ALLOW_HOSTS`, so the
limit is enforced by the network rather than only by AIForge's own code — a `curl` typed
into the agent's shell cannot talk its way past it. The UI is published by a separate
nginx container, since a container on an internal network cannot publish a port.

One cost: the model endpoint is no longer at `127.0.0.1` — point `AIFORGE_LM_BASE_URL` at
`http://host.docker.internal:1234/v1` and allowlist it. See [`docker-compose.isolated.yml`](docker-compose.isolated.yml).

### Host folders

The sandbox sees only `~/.aiforge`. Mount a folder at the same path with `./run.sh
--mount DIR` (repeatable); `--repos DIR` mounts your projects folder as the project root.

The chat can *request* a folder (`mount_folder` / `unmount_folder`), recorded in
`~/.aiforge/mounts.list` — but the request is not the grant. Widening access needs the
**host** to approve it (`./run.sh --mount DIR`, or a `y` at the prompt), and approvals live
outside anything the box can write to, so it cannot grant itself access.

### Flags

| Flag | What it does |
|---|---|
| `--port N` / `--host H` | change the bind (off-loopback needs `AIFORGE_API_TOKEN`) |
| `--dev` | uvicorn hot reload |
| `--isolated` | internal network, all egress through the allowlist proxy |
| `--stop` / `--logs` / `--shell` | stop, follow, or open a shell in the sandbox |
| `--repos DIR` | mount your projects folder (default `~/.aiforge/repos`) |
| `--mount DIR` | approve and mount another host folder; repeatable |
| `--skip-web` | don't rebuild the web UI |
| `--test` | probe the configured model endpoint, then exit |
| `--install-model2vec` | add semantic recall (static embeddings, no torch) |
| `--with-langfuse` / `--stop-langfuse` | the optional self-hosted trace UI |
| `--admin` / `--spoke` / `--admin-url URL` / `--group NAME` | memory-fleet role (exactly one admin) |
| `--migrate` / `--reset-config` | re-converge a prior install; wipe saved agent config (backed up) |
| `--dedupe` / `--recompact-all` / `--migrate-okf` / `--purge-code` | memory maintenance, then exit |

(`--lite` / `--hybrid` / `--no-build` are accepted but do nothing.)

Recall is **keyword + spell-correction** by default, no download. Add vector KNN with
`--install-model2vec`, or an embeddings endpoint you already run:
`AIFORGE_EMBED_BACKEND=api AIFORGE_EMBED_API_MODEL=<model>`.

**Behind a corporate CA?** Paste the root **and its intermediates** into Settings → *Local
certificate authority*, or set `AIFORGE_CA_BUNDLE=/path/ca.pem` — one answer for the model
endpoint, Jira/Confluence/GitLab, `git` and the installs. Verification is never turned off:
a self-signed endpoint is pinned, not trusted blindly.

> ⚠️ **Security.** The agent has full rights inside its box, but reaches only `~/.aiforge`
> plus folders you approved and mounted. The one way out of that is `AIFORGE_IN_SANDBOX=1` —
> how the box runs itself internally, and how a host service such as
> `scripts/runtime/nuc/aiforge-api.service` runs. There it has **full, unsandboxed filesystem
> and shell access**: set `AIFORGE_WORKSPACE_DIR=/path` and treat it like a terminal.

## Features

- **Ticket → PR pipeline** — a ticket runs a multi-agent flow (triage → enhance → plan →
  verify → doer-loop → learn → validate) and opens a PR. Multi-file builds decompose into
  **parallel per-subtask runs** (default on, 4 workers), each in its own fresh context and
  git worktree, all built against a shared **SPEC.md**.
- **Chat coding agent** — **simple** (one agent; a multi-file *build* auto-routes through
  the pipeline; multi-part asks get a live checklist), **plan** (read-only), **team** (the
  full pipeline, ticketless). Answers **stream** in every mode, over a plain-text ReAct
  protocol — so any OpenAI-compatible backend works, with or without native tool-calling.
- **Self-provisioning sandbox** — the box installs what a task needs instead of failing:
  apt tools (tmux, chromium, xvfb, …) via `ensure_runtime`, and a missing Chromium build is
  fetched on first browser launch.
- **Integrations** — a broad **Jira** suite (issues, transitions, time tracking,
  boards/sprints, dashboards), **Confluence**, **GitLab**, **email**, **web fetch/crawl** (a
  URL you supply — there is no web *search* tool), **browser**, **MCP**. `context_gather` builds
  a cached cross-entity **dossier**; all of it callable from shell scripts via the
  **`aiforge-tool`** CLI (read-only by default).
- **Context workspaces** — durable work gets a folder shared across sessions: `~/.aiforge/work/{jira,confluence,repo,web}/<key>/`.
- **Skills, workflows & rules** — reusable playbooks + always-on rules, relevance-matched
  and auto-injected; rules and matched workflows are **mandatory**, and workflow scripts
  pass a hard run-before-save gate. A chat turn that *did* something is **auto-captured**
  into a skill or workflow (LLM-verified as reusable, deduped before writing), and a
  scheduled sweep **merges near-duplicates** across all three — archiving, never destroying.
- **Human-in-the-loop** — per-tool **allow/ask/deny** policy + a command risk classifier;
  risky actions and external writes pause for **Approve/Reject** with a diff preview.
  Autonomous ticket runs never block.
- **Memory** — an **OKR-DAG**: markdown nodes (objectives → key results → learnings →
  sessions) with typed frontmatter edges build an in-memory graph (no DB), and *surgical*
  retrieval feeds the active goal's why/what/rules/recent into the prompt. Sessions auto-author
  durable nodes, compacted hourly; plus a session **execution ledger**, over vector+text+graph
  recall in SQLite. Details: **[SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md#4-memory--knowledge)**.
- **Context engineering** — auto-compaction near the window limit, fresh per-turn memory
  recall + repo-map injection, **Cave mode** (lean context for small local models).
- **Observability** — optional self-hosted **Langfuse v2** trace mirror (SDK-free REST; one app
  container + Postgres, no ClickHouse): every LLM call and memory recall/write, with sessions and
  a per-turn score. `--with-langfuse` hosts it (UI :3005, keys auto-generated, traces pruned to
  1 day). On-disk traces under `~/.aiforge/` regardless.
- **Providers** — automatic cloud escalation, and a model registry with per-model vision + context window.
- **Resilient streaming** — navigate away and back mid-turn; cancel/abort; workspace
  checkpoints with one-click restore; attachments (image/pdf/xlsx/docx) with vision captioning.

## Agents

A ticket (or team chat) flows through specialized agents, each on the model you pick:

- **Triage** → **Enhancer** → **Architect** → **Planner** → **Verifier** (multi-axis plan
  critic: correctness / scope / risk) → **Doer ↔ Refiner ↔ Feedback** (build loop in an
  isolated worktree) → **Validator** → **Live-verifier** (runs the real recipe) →
  **Learner** (writes memory back).
- **Context gatherers** run in parallel: Researcher, Repo-map (AST PageRank), Conventions.
- Per-agent allowlists (and which stages are tool-less) are in
  **[docs/TOOLS.md](docs/TOOLS.md#which-agent-gets-which-tools)**.

## Coding agent tools (summary)

| Group | Tools |
|-------|-------|
| **Files** | `file_read` · `file_write` · `file_create` · `file_patch` · **`editor`** (syntax-checked, undo) · **`multi_edit`** (atomic batch) · `list_dir` |
| **Search / code** | `grep` · `find` · **`lsp`** · `rename_symbol` · `codegraph_*` · `run_command` · **`run_tests`** · `typecheck` · `format` · `execute_ipython_cell` · `project` · `serve` |
| **VCS** | `git_status` / `git_diff` / `git_log` / `git_blame` · **`github_pr`** · `gitlab_mr_create` |
| **Integrations** | `jira_*` · `confluence_*` · `gitlab_*` · `email_*` · `web_fetch` / `web_crawl` (URL you supply — no web search) · `browse` · `mcp` · **`context_gather`** · resolvers |
| **Sandbox** | `ensure_runtime` · `mount_folder` / `unmount_folder` · `save_secret` |
| **Memory / learning** | `memory_lookup` / `memory_write` · `remember_rule` · `skill_search` / `learn_skill` · `workflow_search` / `learn_workflow` |
| **Progress** | **`plan_progress`** (live checklist for multi-part asks) |

Every tool with args, gating and per-agent access: **[docs/TOOLS.md](docs/TOOLS.md)**.

Writes show a **diff**; risky commands and external writes are **approval-gated** by
default; blanket `git add -A` is refused; destructive deletes need confirmation.

## Configuration

Everything is configurable from the UI; env vars override at read time. Full annotated
list: **[.env.example](.env.example)**. Quick hits:

```
AIFORGE_<ROLE>_PROVIDER / _MODEL / _BASE_URL   per-role model routing
AIFORGE_WORKSPACE_DIR            clamp file/exec to one dir (security)
AIFORGE_TOOL_POLICY              e.g. "run_command=ask,file_write=deny"
AIFORGE_EGRESS_ALLOW_HOSTS       CSV allowlist; the --isolated proxy enforces it
AIFORGE_CAVE_MODE                1 = lean context for small local models
AIFORGE_PARALLEL_SUBTASKS(_MAX)  pipeline fan-out (default on, 4)
AIFORGE_ARTIFACT_MERGE=0         turn off the duplicate-merge sweep
AIFORGE_API_TOKEN                required to bind off loopback
JIRA_BASE_URL / JIRA_TOKEN       Jira (also in UI → Settings → Integrations)
CONFLUENCE_BASE_URL / _TOKEN     Confluence (same pattern; _USER ⇒ Basic auth)
```

Jira/Confluence **writes** are approval-gated; reads never prompt. Unconfigured tools degrade
cleanly (`*_not_configured` + a hint), never crash.

## Project layout

```
aiforge_core/
  agents/         archetypes + agents.yaml (per-role tools / scopes / contracts)
  runtime/        chat agent, tools, memory wiring, guards, skills + rules
  orchestrator/   the ADK pipeline
  api/            FastAPI app + routes (port 8799)
  memory/         unified_query (vector + full-text + graph) + decay + store
  integrations/   optional lib adapters — thin seams, graceful without the lib
  net/ config/    egress allowlist + CA handling; providers, env, roles
  jobs/ recipes/ tickets/ workflows/ indexing/ observability/ llm/ cli/
packages/aiforge_memory/   standalone memory package (chunking, embeddings)
web/              React UI
```

## Security rails

- **Workspace clamp** — `AIFORGE_WORKSPACE_DIR` confines file/exec to one dir.
- **Network isolation** — `--isolated` enforces the egress allowlist at the network.
- **Mount approval** — widening the box's view of the host needs host-side approval.
- **Approval gate** — risky / `ask`-policy actions pause for Approve/Reject.
- **Delete guard** — destructive deletes refused unless explicitly confirmed.
- **Scope guard** — Doer edits blocked outside the ticket's `scope_allowlist_globs`.
- **Plan mode** — read-only; proposes a plan before any write.
</content>
