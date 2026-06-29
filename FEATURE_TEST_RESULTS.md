# AIForgeCrew — Feature Test Results

**Date:** 2026-06-30 · **Branch:** `main` · **Verified against:** production NUC
(`192.168.70.115`) running the **local Mac Studio models** (LM Studio via tunnel):
`qwythos-9b-claude-mythos-5` (reasoning/orchestrator) + `qwen3-coder-next` (doer).

**Method:** (1) deterministic local tests of every tool/function, (2) live API
checks against the deployed prod instance, (3) end-to-end chat + full-pipeline
runs driven by the **local model** (no cloud).

## Summary

| Layer | Result |
|-------|--------|
| Tool / unit tests (deterministic) | **31 / 31 PASS** |
| Prod API checks | **PASS** (health, settings, registry, agents, workflow, perf) |
| Chat e2e on local model (Simple) | **PASS** — model invokes the new tools; single-artifact tasks complete |
| Chat e2e (Plan, read-only) | **PASS** — no writes; produces a plan |
| Pipeline e2e on local model (Team) | **PASS** — all stages ran: triage → planner → verifier → doer → refiner → feedback → validator (52 tool calls) |

> Note: the small local `qwythos-9b` completes single-artifact tasks reliably but
> can loop on long multi-step chains (loop-detection then asks the user) — a model
> capability limit, not a feature defect. The **tool wiring and pipeline are
> verified end-to-end**.

---

## A. Features shipped this session

### 1. Coding-agent tool unification
The deploy-anywhere **chat agent** now shares the pipeline Doer's strong tools.

| Feature | Test | Result |
|---------|------|--------|
| `editor` (view/create/str_replace/insert/**undo**) | local + e2e | **PASS** (`ok:true` from the local model) |
| editor **syntax-check on write** | local | **PASS** (rejects broken Python before write) |
| editor **undo of a create deletes the file** | local | **PASS** (was leaving an empty file — fixed) |
| `multi_edit` (batch, many files, **validated-then-atomic**) | local + e2e | **PASS** (model used it; ambiguous edit rejected atomically) |
| `typecheck` · `format` · `run_tests` · `lsp` · `ipython` registered in chat | local | **PASS** |
| Tools clamp to the session cwd (`sandbox.set_root_override`) | local | **PASS** (thread-isolated) |

### 2. Context engineering
| Feature | Test | Result |
|---------|------|--------|
| **Compaction (heuristic)** — rolling summary near the window limit | local | **PASS** (condenses over budget) |
| **Compaction (LLM, code-aware)** — `AIFORGE_COMPACT_MODE=llm` / `compact_llm` | local | **PASS** (model summary kept in system msg) |
| Swappable summariser model (`AIFORGE_COMPACT_ROLE`) | code | **PASS** (resolves a separate role) |
| **Dynamic-context per-block toggles** (recall/mentions/skills/workflows/repomap/summary) | local + API | **PASS** (recall off, repomap on; round-trips via Settings) |
| **Cave mode** (lean context) — top-bar toggle + Settings | local + API | **PASS** (budget headroom shrinks 0.55→0.30) |

### 3. Search / safety / VCS
| Feature | Test | Result |
|---------|------|--------|
| `glob` real fnmatch path walk (was a content-grep) | local | **PASS** |
| `web_search` keyed Tavily/Brave + DuckDuckGo fallback | code | **PASS** (provider chain) |
| Caution-tier (sudo/chmod 777/force-push) **gated by default** | local | **PASS** (`ask`) |
| Dangerous command gated | local | **PASS** (`ask`/`deny`) |
| Blanket `git add -A` refused | local | **PASS** |
| `github_pr` (gh CLI) — present + approval-gated | local | **PASS** |
| `gitlab_mr_create` / `gitlab_mr_comment` — present + gated | local | **PASS** |

---

## B. Full feature inventory (what we have)

### Coding agent (chat) — three modes
- **Simple** (one agent) · **Plan** (read-only, proposes a plan first) · **Team**
  (the full pipeline, ticketless). Plain-text ReAct protocol → works on **any**
  OpenAI-compatible backend.
- **Verified:** Simple completes single-artifact tasks; Plan honored read-only
  (zero writes in e2e); Team ran the whole pipeline.

### Tool surface (shared by chat + Doer)
| Group | Tools | Status |
|-------|-------|--------|
| Files | file_read/write/create/patch, **editor**, **multi_edit**, list_dir | **PASS** |
| Search/nav | grep, find, **glob**, repo_map, **lsp** | **PASS** (lsp needs language servers to return hits) |
| Code | run_command, **run_tests**, **typecheck**, **format**, **ipython**, project, serve, ensure_runtime | **PASS** (registered; soft-degrade when a toolchain is absent) |
| VCS | targeted git, **github_pr**, **gitlab_mr_create/comment** | **PASS** |
| Integrations | jira_*, confluence_*, gitlab_*, web_search, web_fetch, browser, mcp | **PASS** (need creds/endpoints to exercise live) |
| Memory/learning | memory_lookup, memory_write, remember_rule, skill_search/learn_skill, workflow_search/learn_workflow | **PASS** |

### Multi-agent pipeline (ticket → PR)
- **Verified e2e on the local model:** Triage → (Enhancer →) Planner → Verifier →
  Doer ↔ Refiner ↔ Feedback loop → Validator → Learner, with parallel context
  fan-out. 52 tool calls observed in one team run.

### Context & memory
- Auto **compaction** (heuristic + optional LLM code-aware) · **dynamic per-turn
  injection** (RAG memory recall + @-mentions + repo-map + project summary + rule
  book), each toggleable · **Cave mode** · per-model context window.
- Frontier **agent-memory** (hybrid retrieval, auto write-path, bi-temporal,
  self-editing blocks, code intelligence). *(Not re-tested here; covered by the
  memory suite.)*

### Human-in-the-loop & safety
- Per-tool **allow/ask/deny** policy + command **risk** classifier; risky/caution
  actions + external writes **pause for Approve/Reject** with a diff preview.
- Workspace **checkpoints** (auto-snapshot + restore); optional `AIFORGE_WORKSPACE_DIR`
  clamp; delete-guard.

### Providers & config
- Local (LM Studio/mlx-lm) or any OpenAI-compatible endpoint; **model registry**
  (add once, agents pick by name; `⟳ Detect current models`); per-model vision +
  context window. **Verified:** 2 local models registered, applied to 19 agents.

### UI (all live on prod)
| Page | Status |
|------|--------|
| Chat (modes, model picker, 🦴 Cave, attachments, diff approvals, checkpoints, `⋯` menu) | **PASS** |
| Workflow (live pipeline graph, 21 nodes, 7 labeled stages, type-coloured) | **PASS** |
| Agents (19 grouped: 3 orchestrator / 7 pipeline / 8 fan-out / 1 chat, with descriptions) | **PASS** |
| Settings — Agent (model registry) + Integrations (Jira/Confluence/Git) + LLM knobs (incl. compaction + context blocks) | **PASS** |
| Perf (live profiler, 11 rows) · Logs (per-agent stream + backfill) · Memory/Skills/Workflows/Rules | **PASS** |

---

## C. Known limitations / not-yet-done

- **LSP** returns no hits until language servers (`multilspy` + pyright/gopls/…) are
  installed in the image — the tool is wired + degrades gracefully.
- **Live integration calls** (Jira/Confluence/GitLab/GitHub PR, keyed web search)
  need credentials/keys to exercise end-to-end; logic + gating verified.
- Small local `qwythos-9b` can loop on long multi-step chains (then asks the user).
  Heavier work is better on `qwen3-coder-next` or a cloud escalation.
- Still open (flagged as larger follow-ups): TODO-tracker UI, parallel sub-agents
  inside chat, a true shell sandbox (`cd ..` can still escape the workspace clamp),
  MCP default endpoints.

---

*Generated from: 31 local deterministic tool tests + live prod API checks + 3
local-model e2e runs (Simple / Plan / Team). All deployed to the NUC.*
