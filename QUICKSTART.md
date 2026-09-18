# AIForgeCrew — Quickstart

From installed → configured → doing real work. Install first:
**[INSTALL.md](INSTALL.md)**, then open **<http://127.0.0.1:8799/ui/>**.

- [1. Add your model](#1-add-your-model)
- [2. Configure integrations](#2-configure-integrations)
- [3. Create a scheduled Job](#3-create-a-scheduled-job)
- [4. Create Rules, Skills & Workflows](#4-create-rules-skills--workflows)
- [5. Memory](#5-memory)
- [6. Do work: Chat & Tickets](#6-do-work-chat--tickets)
- [Data, security & where things live](#data-security--where-things-live)

---

## 1. Add your model

On the home page (**Settings → Agent**), in the **Models** card:

1. Paste your model server's address into **Base URL**:
   - LM Studio on this computer: `http://127.0.0.1:1234/v1` on Linux,
     `http://host.docker.internal:1234/v1` on macOS/Windows
   - Ollama, vLLM, OpenRouter, Groq, a cloud endpoint: its `/v1` URL (plus **API key** if it needs one)
2. Press **🔍 Identify models from URL** and click the model you want, or type
   its **Model id** and press **+ Add model**.

That is all: AIForge decides which agent uses which model. Add a second, stronger
model later if you like. **🔧 Test tools** on a model's row checks that it answers.

Model on a server with an internal TLS certificate: see
[docs/ADVANCED.md](docs/ADVANCED.md#behind-a-corporate-ca-or-proxy).

---

## 2. Configure integrations

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

## 3. Create a scheduled Job

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

## 4. Create Rules, Skills & Workflows

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

## 5. Memory

AIForge remembers what it learns, as readable Markdown under `~/.aiforge/memory/`.
To give it a repo's code and docs up front: **Memory** page → **Add source**. The
agents recall memory automatically before searching files. Details:
[docs/OKR_MEMORY.md](docs/OKR_MEMORY.md) and [docs/ADVANCED.md](docs/ADVANCED.md#memory-details).

---

## 6. Do work: Chat & Tickets

- **Chat** — ask for anything in your code. Easiest: run `aiforge` in the project
  folder. In the web app, start the chat in that folder (the 📁 button) so it works
  there.
- **Tickets** — describe a change in plain words; AIForge plans it, writes the
  code, tests it and opens a pull request.

---

## Data, security & where things live

- **Your data:** everything (settings, chats, memory, tickets) is under `~/.aiforge/`.
- **Security:** the agent has full rights inside its sandbox, but from your
  computer it sees only `~/.aiforge` and folders you approved (`aiforge mount add`).
- More detail (tokens, reverse proxies, `/admin`): [docs/ADVANCED.md](docs/ADVANCED.md).
