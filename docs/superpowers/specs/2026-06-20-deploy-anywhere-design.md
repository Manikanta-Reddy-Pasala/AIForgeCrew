# AIForge — Deploy-Anywhere Design

**Date:** 2026-06-20
**Status:** Approved (brainstorming) — pending implementation plan
**Goal:** Un-Milo-ify AIForgeCrew. Cut the Mac-Studio / NUC / Neo4j / Postgres hard
wiring so anyone can `git clone && ./run.sh`, pick a provider on a home page, chat or
file tickets, and let the agent work with full filesystem freedom.

---

## 1. Problem

Today AIForgeCrew only runs on the operator's own infra:

- API backend (`aiforge_core/api/api.py`) hard-imports `psycopg` → **Postgres required** for
  tickets/state.
- Memory (`aiforge_core/memory/`) hard-binds **Neo4j + Postgres + embed/rerank sidecars**.
- Provider config (`config/agent_config.py`) is env/operator-driven; the LM Studio `local`
  provider points at a fixed `:1234`.
- UI is tickets-first; Chat view exists but is not wired to a full agent.
- No one-command deploy; setup assumes specific machines.

Result: nobody but the author can stand it up.

## 2. Decisions (locked in brainstorming)

| # | Decision |
|---|----------|
| Repo strategy | **Refactor in place.** Reuse existing machinery, no rewrite. |
| Memory backend | **Embedded default + Neo4j opt-in.** SQLite + local vector index by default; full graph backend when `NEO4J_URL` is set. |
| Provider menu | **Generic OpenAI-compatible** (base_url + optional key) **+ Ollama**. Covers OSS-no-token, LM Studio, OpenRouter, Groq, Together, vLLM, and cloud-with-key. Existing anthropic/gemini/claude_local stay in tree as advanced, off the home menu. |
| Chat behavior | **Conversational coding agent with full FS.** Lightweight ReAct loop, not the ticket pipeline. |
| Permission scope | **Total freedom by default** (whole machine, no gating). Optional `AIFORGE_WORKSPACE_DIR` clamp. README warns + recommends container for shared deploys. |
| Deploy form | **One script: `git clone && ./run.sh`.** Embedded SQLite, no Docker needed. Dockerfile optional, later. |

Two operator confirmations:
- **A.** Tickets get a SQLite backend too (not just memory) — required because the API hard-needs Postgres today.
- **B.** Embedded memory is degraded (vector recall only; no graph-hop / domains / tours). Full graph only with Neo4j set.

## 3. Architecture

Single FastAPI process serves the built React UI (static files) **and** the REST/SSE API on
one port (`:8799`). One `./run.sh` boots everything.

```
git clone → ./run.sh → http://localhost:8799
   ├── Home (config)   ← pick provider + model per pipeline step
   ├── Chat            ← full-FS conversational agent
   └── Tickets         ← existing pipeline (unchanged)
```

No NUC, no Mac Studio, no external DB assumptions in the default path.

## 4. Storage abstraction

Introduce a backend interface with two implementations selected by env presence.

| Layer | Default (zero infra) | Pro (opt-in) |
|-------|----------------------|--------------|
| Tickets / state | **SQLite** at `~/.aiforge/aiforge.db` | Postgres when `AIFORGE_PG_URL` set |
| Memory | **SQLite + sqlite-vec** local vector index | Neo4j when `NEO4J_URL` set |

### 4.1 Tickets
- Define the store contract in `aiforge_core/tickets/store.py` (extract interface from current
  Postgres logic).
- Add `store_sqlite.py` implementing the same contract over SQLite.
- A factory selects backend: Postgres if `AIFORGE_PG_URL`, else SQLite.
- `api.py` and all callers depend on the interface, not `psycopg` directly.

### 4.2 Memory
- `memory/__init__.py` gains a `backend` switch.
- Embedded backend: store memory units + embeddings in SQLite; recall via vector similarity
  (sqlite-vec or a pure-Python cosine fallback). Skips graph-hop, domains, flows, tours.
- Neo4j path unchanged and used automatically when `NEO4J_URL` is present.
- Embedding: default to a small local embed (or hash-embed fallback) so no sidecar / GPU is
  needed. Embed/rerank sidecars remain opt-in.

### 4.3 Migration
- No auto-migration of existing Postgres/Neo4j data into SQLite. Pro users keep their stack by
  setting the env vars. Fresh users start empty on SQLite.

## 5. Provider layer

- Generalize `providers/local.py` into `providers/openai_compatible.py`:
  reads `base_url`, optional `api_key`, and `model` **from the agent_config JSON** (set via the
  UI), with env vars still overriding at read time.
- Single provider entry covers: LM Studio, OpenRouter, Groq, Together, vLLM, and
  Anthropic/OpenAI compat endpoints. "No token" = blank key.
- Keep an `ollama` entry (local + cloud).
- `anthropic`, `gemini`, `claude_local` remain registered as advanced providers but are not
  surfaced on the home menu.
- Add a **test-connection** path: given base_url/key/model, hit `/v1/models` (or a 1-token
  completion) and report reachable / model-present.

## 6. Home page + Chat

### 6.1 Home (config-first landing)
- New default route. One card per pipeline step (planner, verifier, doer, feedback, learner,
  …): provider dropdown (OpenAI-compatible | Ollama), base_url, api_key, model, and a
  **Test connection** button.
- "Apply to all steps" bulk action for one-shot configuration.
- Persists to `agent_config.json` via the existing config API.
- Reuse existing `Settings.tsx` / `Agents.tsx` logic; restyle as the landing surface.

### 6.2 Chat (conversational coding agent)
- New `POST /api/chat` SSE endpoint driving a ReAct loop.
- Reuse `aiforge_core/agents` + the Doer tool set: `file_read`, `file_write`, `file_patch`,
  `file_create`, `run_command`, `memory_lookup`.
- Streams assistant tokens + tool-call events to the existing `Chat.tsx`.
- Each session has a working directory (defaults to repo root; configurable).
- Chat turns are written to memory (embedded or Neo4j, whichever is active).

## 7. Permissions + deploy

### 7.1 Permissions
- Default: **total freedom** — agent tools perform no path or command gating.
- Optional `AIFORGE_WORKSPACE_DIR` clamps create/read/write/exec to that root for cautious
  users.
- README carries a bold warning about unrestricted exec and recommends running shared deploys
  inside a container.

### 7.2 `run.sh`
- Detect / create uv venv → `uv sync`.
- Build web if `web/dist` is stale: `npm ci && npm run build` (skip if dist current).
- Launch `uvicorn` serving UI + API on `:8799`.
- `--dev` flag for hot reload.
- Optional `Dockerfile` is a later, non-blocking extra.

## 8. Phasing

Each phase is its own commit with tests green.

1. Storage interface + SQLite tickets backend (API no longer hard-needs Postgres).
2. SQLite + vector memory backend + embed fallback.
3. `openai_compatible` provider + config-driven base_url/key/model.
4. Home page (config landing) + provider test-connection endpoint.
5. Chat SSE endpoint + full-FS ReAct loop + `Chat.tsx` wiring.
6. `run.sh` one-command boot + README + permission warning.

## 9. Testing

- Storage: contract tests run against both SQLite and Postgres backends (Postgres path skipped
  when env unset).
- Memory: embedded backend unit tests (insert + vector recall); Neo4j path behind a
  `live_neo4j` marker.
- Provider: `openai_compatible` endpoint resolution + test-connection against a stub server.
- Chat: ReAct loop tool dispatch unit tests with a fake LLM.
- Smoke: `run.sh` boots, `/api/health` green, home page loads, a chat turn writes a file in a
  temp workspace.

## 10. Out of scope

- Auto-migration of existing Postgres/Neo4j data.
- Multi-user auth / RBAC.
- Hosted/cloud SaaS deployment.
- Graph features (domains/flows/tours) on the embedded backend.
