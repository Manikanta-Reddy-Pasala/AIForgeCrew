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

**Prereqs:** Docker (for the default + `--docker` modes), Node + npm (to build the
UI), and one reachable model endpoint (e.g. LM Studio, OpenRouter, a cloud key).

```bash
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew.git
cd AIForgeCrew
./run.sh                 # DEFAULT = hybrid
```

Open **http://127.0.0.1:8799/ui/**.

Three run modes:

| Command | What runs where | Use when |
|---|---|---|
| `./run.sh` (default) | **hybrid** — Postgres + Neo4j + embed + rerank in Docker; **api + UI + runner on the host** | You want the agent to see the host filesystem/tools (coding). |
| `./run.sh --docker` | **everything in containers** (agent isolated to the mounted workspace) | Shared / untrusted deploy. |
| `./run.sh --lite` | **all on the host, embedded SQLite**, no Docker | Fastest "just try it", no infra. |

Handy flags: `--port N` · `--host 0.0.0.0` (LAN — needs `AIFORGE_API_TOKEN`, or
`AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1` if you front it yourself) · `--dev` (hot
reload) · `--reset-config` (wipe saved model config) · `--test` (probe the model
endpoint and exit).

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

## 6. Index your code into Memory

**Memory** page → **Add source** → point at a repo or docs folder. Indexing populates
four layers (Neo4j backend):

- **Tree-sitter symbols** — classes/methods + call/extends/implements edges
- **Code / doc chunks** — embedded content for semantic recall
- **Graphify** — a concept graph
- **Facts** — observations/decisions the agents write during runs

Then: **search across everything**, hit **Preview graph** / **Explore** for an
in-app interactive graph (pan/zoom, click a node to expand), or **Open in Neo4j
Browser**. The chat/coding agents recall this memory automatically ("memory-first")
before searching files.

---

## 7. Do work: Chat & Tickets

- **Chat** — a full-filesystem coding agent. For work on a specific repo, start the
  chat **rooted at that repo** (the 📁 "new chat with a working directory" button) so
  it operates there instead of an empty scratch dir. It uses memory + LSP + tests.
- **Tickets** — file a plain-language ticket; the pipeline runs it end to end:
  triage → enhance → plan → code (Doer loop with verify) → validate → learn → PR.

---

## Data, security & where things live

- **Storage:** `--lite` = SQLite under `~/.aiforge/`. Hybrid/`--docker` = Postgres
  (tickets/chat/jobs) + Neo4j (memory). Set `AIFORGE_PG_URL` / `NEO4J_URI` to force
  the pro backends.
- **Config + user data:** `~/.aiforge/` (agent config, chat db, jobs, skills,
  workflows, rules, memory).
- **Security:** by default the chat agent has **full unsandboxed filesystem + shell**
  on the host. Set `AIFORGE_WORKSPACE_DIR=/path` to clamp it, and use `--docker` for
  shared/untrusted deploys. Binding non-loopback (`--host 0.0.0.0`) requires
  `AIFORGE_API_TOKEN` (or the explicit `AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1` opt-out
  when you front it with your own auth/tunnel).
