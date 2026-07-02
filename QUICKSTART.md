# AIForge — Quickstart

Run it, point it at a model, start chatting / building. No cloud account needed —
works fully offline with a local model.

---

## 1. Run it

**Simplest (zero infra — embedded SQLite, no Postgres/Neo4j):**

```bash
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew.git
cd AIForgeCrew
./run.sh
```

Open **http://127.0.0.1:8799/ui/**

Flags: `./run.sh --dev` (hot reload) · `--port N` · `--host H` · `--skip-web`.

**Full stack (Postgres + Neo4j + sidecars) via Docker:**

```bash
docker compose up -d --build      # everything on :8799
```

---

## 2. Point it at an offline model

AIForge talks the **OpenAI-compatible** API, so any local server works. Pick one:

### Option A — LM Studio (easiest desktop)
1. Install [LM Studio](https://lmstudio.ai), download a model (e.g. `qwen/qwen3-coder-next`).
2. **Load** the model and start its server: **Developer → Start Server** (defaults to `http://localhost:1234/v1`).
   - Keep it loaded: LM Studio idle-unloads after a while — set a long TTL, or just re-load before use.
3. In AIForge → **Home** page, for each step pick provider **OpenAI-compatible**, base URL `http://localhost:1234/v1`, leave the key blank, and choose the model. Hit **Test connection**.

### Option B — Ollama
1. `ollama serve` + `ollama pull qwen2.5-coder`.
2. Home page → provider **OpenAI-compatible**, base URL `http://localhost:11434/v1`, key blank, model `qwen2.5-coder`.

### Option C — vLLM / llama.cpp / LocalAI / any OpenAI-compatible server
- Home page → **OpenAI-compatible**, paste its `…/v1` base URL, key blank (or a token if it needs one), model id.

### Option D — a cloud endpoint
- Same **OpenAI-compatible** entry, paste the cloud base URL + your API key, pick the model. (OpenRouter, Groq, Together, etc.)

> **"No token"** = leave the API-key field empty. That's the OSS/local case.

---

## 3. Use it

### Chat (like a coding CLI, in the browser)
- **Chat** page → **New chat**.
- **Model** dropdown shows only models that are **loaded right now** — a green ● means active. Set it on the Home page's **Chat** row too.
- **Simple** mode: one fast agent that reads/writes files + runs commands.
- **Team (full flow)** mode: the full agent pipeline (planner → verifier → doer → feedback → learner) for bigger builds.
- Each chat session gets its **own isolated workspace** — it can create files, run, and clean up there safely. A ⏱ timer shows how long each turn takes.

### Tickets (tracked pipeline runs)
- **Tickets** page → file a ticket; the pipeline runs it end-to-end. Watch progress on the ticket / **Workflow** view.

### Memory
- **Memory** page: see what's indexed, and add **sources** — code repos, docs/markdown folders, URLs, or uploaded files — then **Index** them so chat + the pipeline can recall them.

---

## 4. Configure models per step

Every pipeline step (planner, doer, verifier, …) and the chat slot has its own model,
set on the **Home** page. Mix and match: a big local coder for the Doer, a small fast
model for triage, a cloud model for review — whatever you've got. "Apply to all steps"
sets one model everywhere at once.

---

## Env knobs (optional)

| Var | What |
|-----|------|
| `AIFORGE_PG_URL` | use Postgres for tickets (else embedded SQLite) |
| `NEO4J_URI` | use Neo4j graph memory (else embedded SQLite vector store) |
| `AIFORGE_WORKSPACE_DIR` | clamp the agent's file/shell access to one dir (default: unrestricted) |
| `AIFORGE_CHAT_WORKSPACE_ROOT` | where per-chat workspaces live (default `~/.aiforge/chat-workspaces`) |
| `AIFORGE_<ROLE>_BASE_URL` / `_MODEL` / `_API_KEY` | per-step override at runtime |

> ⚠️ By default the chat/agent has **full filesystem + shell access**. Set
> `AIFORGE_WORKSPACE_DIR` to clamp it, or run in a container for shared use.
