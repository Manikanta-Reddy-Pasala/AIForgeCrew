# AIForgeCrew — Quickstart

Everything you need to go from clone → running → configured → doing real work.
**Read this first.**

- [1. Run it](#1-run-it)
- [2. Configure models](#2-configure-models)
- [3. Configure integrations](#3-configure-integrations)
- [4. Create a scheduled Job](#4-create-a-scheduled-job)
- [5. Create Rules, Skills & Workflows](#5-create-rules-skills--workflows)
- [6. Index your code into Memory](#6-index-your-code-into-memory)
- [7. Do work: Chat & Tickets](#7-do-work-chat--tickets)
- [Data, security & where things live](#data-security--where-things-live)

---

## 1. Run it

**Prereqs:** Node + npm (to build the UI) and one reachable model endpoint (LM
Studio, vLLM, Ollama, OpenRouter, a cloud key…). **No Docker required** — the
stack is single-mode: embedded SQLite + Markdown OKR memory, all on the host.
(Docker is only needed for the optional self-hosted Langfuse trace UI.)

```bash
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew.git
cd AIForgeCrew
./run.sh
```

Open **http://127.0.0.1:8799/ui/**. First boot builds the venv + UI and starts
the api + team-pipeline runner on the host — it does **not** download anything
heavy, so it comes up fast.

**Upgrading an OLD install** (a previous dockerized Postgres/Neo4j setup)? Just
`git pull && ./run.sh` — the first boot AUTO-migrates your data (Postgres →
SQLite tickets/chat, Neo4j facts → OKR briefs, briefs → `compacted/` folder, okf
DAG → `memory-archive/`) and removes the DB-infra containers (keeps Langfuse).
Force a re-converge anytime with `./run.sh --migrate`. No data loss; nothing to
hand-edit.

**Memory recall — hash (default) vs semantic:**

| Backend | What it does | Cost |
|---|---|---|
| **hash** (default) | keyword / exact-id / spell-correction. Fully works — briefs, migration, chat, contradiction-resolve, seed-index, lint, hot-cache. | none — no heavy download |
| **model2vec** (opt-in) | adds meaning/paraphrase vector KNN ("how do we ship a release" → the deploy brief, zero shared words) | static embeddings, ~30 MB model, **no torch** |
| **api** (opt-in) | semantic from an OpenAI-compatible `/v1/embeddings` endpoint you already run (LM Studio / Ollama) | no local model at all |

Enable semantic **once** (installs model2vec, then starts with it active):
```bash
./run.sh --install-model2vec
```
Afterwards every plain `./run.sh` auto-detects it. Force hash:
`AIFORGE_EMBED_BACKEND=hash ./run.sh`. Or the API backend:
`AIFORGE_EMBED_BACKEND=api AIFORGE_EMBED_API_MODEL=<embed-model> ./run.sh`.

Handy flags: `--port N` · `--host 0.0.0.0` (LAN — needs `AIFORGE_API_TOKEN`, or
`AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1` if you front it yourself) · `--dev` (hot
reload) · `--reset-config` (wipe saved model config) · `--test` (probe the model
endpoint and exit) · `--migrate` (force re-converge) · `--install-model2vec`
(one-time semantic install, no torch) · `--recompact-all` (re-fold every brief, then exit).

> `--lite` / `--hybrid` / `--docker` / `--no-build` are legacy no-ops — the stack
> is always single-mode SQLite now.

---

## 2. Configure models

The landing page and **Settings → Agents** are config-first. Each pipeline role
(planner, doer, feedback, learner, supervisor) and the chat agent can point at its
own model.

1. **Pick a provider.** Choose **OpenAI-compatible** and paste any base URL:
   - LM Studio: `http://localhost:1234/v1` (leave key blank)
   - vLLM / Ollama / LocalAI / Together / Groq / OpenRouter / a cloud endpoint (+ key)
2. **Test connection** — verifies reachability + that the served model answers.
3. **Register a model** (Settings → Agents → add) with a label + the model id the
   endpoint serves (e.g. `qwen/qwen3-coder-next`).
4. **Apply it to roles** — assign a model per role, or use a **profile** to switch
   all roles at once. The chat agent's model is set the same way.

> Tip: for a local setup, register several models (a fast coder + a bigger
> reasoner) and assign the strong one to `planner`/`doer`. TLS on an internal
> self-signed endpoint? set `AIFORGE_LLM_SSL_VERIFY=false`.

---

## 3. Configure integrations

**Settings → Integrations** — connect tools the chat agent can then search/read/write
(writes go through the chat approval gate):

| Tab | Fill in | Gives the agent |
|---|---|---|
| **Jira** | Base URL + Personal Access Token (or basic auth) | search / read / create / update / comment on issues |
| **Confluence** | Base URL + token | search / read / create / update pages |
| **GitLab** | Base URL + token | search / read / open & comment on MRs |
| **Email** | SMTP host/port/user/pass/from + IMAP host/port/user/pass | send + read/search email |

Hit **Test connection** on each. Secrets are write-only (never shown back), and an
env var of the same name always overrides the stored value.

---

## 4. Create a scheduled Job

**Jobs** page. A job fires on a cron schedule. Two kinds:

- **Ticket job** — each fire creates a ticket the agent pipeline runs. Good for
  "write code / do research"-style recurring work.
- **Script job** — each fire runs a **deterministic shell script** (no LLM). Good
  for ops: pull repos, back up a DB, rotate a log.

**Easiest: build one by chatting.** Jobs → **New job via chat**. Describe the task;
the builder interviews you, drafts the script, **dry-runs it**, and on your approval
schedules it (the script is saved under `~/.aiforge/jobs/`, and only scripts in that
folder are ever executed, with a timeout).

---

## 5. Create Rules, Skills & Workflows

Go to the **Library** page — Skills / Workflows / Rules each have their own screen,
with a **Default** tab (built-in, ships with AIForge) and a **Custom** tab (yours).
On each screen, create from the form or with **"New … via chat"** (a guided builder
that interviews you and saves it):

- **Rules** — always-on coding constraints the agents must obey (e.g. "match existing
  conventions", "no debug artifacts"). Scope: global or per-repo.
- **Skills** — reusable how-to playbooks the agents pull in **automatically** when a
  task matches the skill's triggers (e.g. "java-spring-boot", "systematic-debugging").
- **Workflows** — end-to-end procedures the agents follow step by step (e.g. "ship a
  feature", "fix a bug", "onboard to a new repo").

---

## 6. Memory

Memory is **scoped OKR briefs** — human-readable Markdown files under
`~/.aiforge/memory/`:

```
~/.aiforge/memory/
├── compacted/          the briefs — one per scope (OKR envelope:
│   ├── compacted-shared.md          Objective / Key Results / Facts / Links / Learnings)
│   ├── compacted-<repo>.md          · shared = global (cross-project)
│   └── compacted-<topic>.md         · <repo> = one project · <topic> = a theme
├── archive/            raw captures, folded + archived (reversible)
└── okf/                (marker only — the old node-DAG is consolidated out)
```

**How it fills:**
- The **Memory** page → **Add source** indexes a repo/docs folder (tree-sitter
  symbols + code/doc chunks + facts) for code context.
- Agents write learnings during runs; each chat **session** distils into briefs
  when it goes idle.
- **Seed a fresh machine** from agent-instruction files — a committed, repeatable
  command (works on hash **or** semantic; no CLAUDE.md needed on other boxes,
  their briefs come from migrated memory):
  ```bash
  # stop the api first, then:
  aiforge-memory-instructions --clear --root <repos-dir>   # CLAUDE.md / AGENTS.md / GEMINI.md
  ```

**Recall** is hybrid + self-maintaining:
- **Search** (Memory page or the agents' `memory_lookup`) fuses semantic vector
  KNN (if installed) + keyword/BM25 + spell-correction, and **follows brief
  Links** to pull related briefs' full text. The API/UI split results into
  **vector** vs **markdown** groups.
- A **seed index** (TOC of every brief) is injected into the chat prompt so the
  agent knows what memory exists to query.
- Housekeeping runs nightly (02:00 local) + hourly: consolidation, **contradiction
  resolve** (a newer fact overwrites a stale contradicting one in any scope),
  cross-scope link mapping, and a graph-health **lint** (dangling links / orphans).
- A **hot cache** surfaces just-written facts immediately, before compaction.

The chat/coding agents recall this memory automatically ("memory-first") before
searching files.

---

## 7. Do work: Chat & Tickets

- **Chat** — a full-filesystem coding agent. For work on a specific repo, start the
  chat **rooted at that repo** (the 📁 "new chat with a working directory" button) so
  it operates there instead of an empty scratch dir. It uses memory + LSP + tests.
- **Tickets** — file a plain-language ticket; the pipeline runs it end to end:
  triage → enhance → plan → code (Doer loop with verify) → validate → learn → PR.

---

## Data, security & where things live

- **Storage:** SQLite under `~/.aiforge/`, in every mode including `--docker`.
  There is no Postgres/Neo4j option any more: the drivers are not installed
  (psycopg/pymongo are absent outright, neo4j is the `aiforge-memory[graph]`
  extra), run.sh strips `AIFORGE_PG_URL` / `NEO4J_URI` from the environment on
  boot, and `deploy/converge.py` comments them out of a stale `.env` and tears
  down any leftover `aiforge-neo4j` / `aiforge-postgres` container. Setting
  those variables does nothing.
- **Config + user data:** `~/.aiforge/` (agent config, chat db, jobs, skills,
  workflows, rules, memory).
- **Security:** by default the chat agent has **full unsandboxed filesystem + shell**
  on the host. Set `AIFORGE_WORKSPACE_DIR=/path` to clamp it, and use `--docker` for
  shared/untrusted deploys. Binding non-loopback (`--host 0.0.0.0`) requires
  `AIFORGE_API_TOKEN` (or the explicit `AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1` opt-out
  when you front it with your own auth/tunnel); the check reads the real socket, so
  a bare `uvicorn --host 0.0.0.0` is refused too. **If a reverse proxy on the same
  host fronts the API, also set `AIFORGE_TRUST_LOOPBACK=0`** — otherwise every
  proxied request looks like `127.0.0.1` and skips the token. `/admin` always needs
  the token when one is set.
