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
- **Chat** — a full-filesystem coding agent. Three modes: **Simple** (one agent),
  **Plan** (read-only — proposes a plan before touching anything), **Team** (the full
  pipeline, ticketless).
- **Human-in-the-loop** — per-tool **allow/ask/deny** policy + a command **risk**
  classifier; risky/ask actions **pause for Approve/Reject** with a diff preview (in
  chat *and* the team pipeline). Autonomous ticket runs never block.
- **Workspace checkpoints** — auto-snapshot before each turn; one-click **restore**.
- **Skills** — reusable `SKILL.md` playbooks (agentskills.io standard), relevance-
  searched and auto-injected; the agent **authors new skills** when it solves
  something (`learn_skill`), which also land in memory.
- **Memory** — hybrid recall (vector + full-text + graph); learns facts, decisions,
  and per-repo project summaries across sessions.
- **Context @-mentions** — `@file`, `@folder`, `@url`, `@problems`.
- **Repo microagents** — repo-shipped conventions auto-injected by trigger word.
- **Providers** — local (LM Studio / mlx-lm) or any OpenAI-compatible endpoint, with
  automatic cloud escalation; per-role model assignment + bulk profiles.

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
AIFORGE_RISK_ASK_CAUTION       1 = also prompt on caution-level commands
AIFORGE_CHAT_AUTO_CHECKPOINT   1 = snapshot before each turn (default)
AIFORGE_CHAT_AUTO_MEMORY       1 = persist a memory note per turn (default)
AIFORGE_SKILLS_DIR             skill registry root (default ~/.aiforge/skills)

# Storage (optional "pro" backends; embedded SQLite by default)
AIFORGE_PG_URL                 Postgres tickets
NEO4J_URI                      graph memory
```

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
