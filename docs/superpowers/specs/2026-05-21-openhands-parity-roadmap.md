# OpenHands Parity Roadmap — Meta Spec

**Date:** 2026-05-21
**Status:** Draft (awaiting user review)
**Owner:** Architect (human-driven)
**Scope:** Decomposition of the "AIForgeCrew agents at OpenHands feature parity" goal into nine ship-sized sub-projects, each with its own design spec → plan → implementation cycle.

---

## 1. Driving question

> "Pick functionalities, check our functionalities, compare, and add missing features."

OpenHands provides a substantially richer agent runtime than AIForgeCrew's current ADK + GA pipeline. Naively trying to land it all in one spec would yield an unbounded plan and a stalled implementation. This meta-spec decomposes the work into nine independent, ship-sized sub-projects, ordered by dependency.

## 2. Current AIForgeCrew agent surface (2026-05-21)

| Role | Runtime | Tools | Memory write |
|---|---|---|---|
| Architect | external Claude Code | read-only repo/graph/memory + ticket CRUD | none |
| Planner | ADK + GA | read-only + write_plan + create_child_ticket | none |
| Verifier | ADK + direct LiteLLM | none (single-completion judge) | none |
| Researcher | ADK + GA | read-only repo/graph/memory | none |
| Doer | ADK + GA | file_read/write/patch + run_shell + grep + fetch + git_commit + memory/graph lookup + checkpoint | none |
| Refiner | ADK + direct LiteLLM | none | none |
| Feedback | ADK + direct LiteLLM | none | none |
| Learner | ADK + direct LiteLLM | none (server-side write_fact plugin) | `:Fact` (only on verdict=pass) |
| Triage | ADK + direct LiteLLM | none | none |

Other live machinery: `EscalatingLlm` primary→cloud demote, `loop_budget` per-agent caps, `sandbox.resolve_inside_root` traversal guard, `syntax_guard` pre-write sniff, `git_pr` end-of-ticket auto-PR, AiForgeMemory hybrid recall (vector + fulltext + graph hop), Neo4j event stream (`:Turn`, `:ToolCall`, `:Fact`).

## 3. Feature gap (OpenHands → AIForgeCrew)

| # | Feature | OpenHands | AIForgeCrew | Verdict |
|---|---|---|---|---|
| 1a | Persistent bash session | `execute_bash` (tmux/pexpect) | `run_shell` stateless | gap |
| 1b | Multi-command editor (view/insert/undo) | `str_replace_editor` | 3 flat tools | gap |
| 1c | Explicit thinking tool | `think` | none | gap |
| 1d | Explicit finish signal | `finish` | inferred | gap |
| 2 | Browser (Playwright) | `browse` + `browse_interactive` | `fetch_url` only | gap |
| 3 | IPython persistent kernel | `execute_ipython_cell` | none | gap |
| 4 | Memory condenser | LLM / recent / amortized-forgetting | raw event stream | gap |
| 5 | Microagents (trigger-keyword) | knowledge / repo / task | hybrid recall but no triggers | partial |
| 6 | Multimodal vision | image inputs | text only | gap |
| 7 | Docker sandboxed runtime | yes | `sandbox.py` path-only | gap |
| 8 | Agent delegation (`AgentDelegateAction`) | yes | orchestrator-level only | partial |
| 9 | Cost/budget per call | `LLMMetrics` | `loop_budget` per-agent | partial |
| - | GitHub issue resolver | yes | `git_pr.py` | OK |
| - | Checkpoint/resume | event-stream replay | `update_working_checkpoint` | OK |

## 4. Sub-project decomposition

| # | Sub-project | Outcome | Depends on | Est. spec size |
|---|---|---|---|---|
| 1 | **Tool surface upgrade** | persistent bash (tmux) + OH editor + think + finish | none | M (this spec) |
| 2 | **Browser tool** | Playwright `browse`/`browse_interactive` as ADK FunctionTool; screenshot return | none | M |
| 3 | **IPython kernel** | persistent Jupyter kernel per session; `execute_ipython_cell`; variable state | #1 (shares session abstraction) | M |
| 4 | **Memory condenser** | event-stream model + LLM/recent/amortized condensers; plug into ADK history | none | L |
| 5 | **Microagents** | keyword-triggered knowledge/repo/task agents; frontmatter `triggers: [..]`; injected mid-prompt | #4 | M |
| 6 | **Multimodal vision** | image inputs (screenshots → LLM); bridge with browser tool | #2 | S |
| 7 | **Docker sandbox runtime** | containerized exec for bash + ipython; toggleable per agent | #1, #3 | L |
| 8 | **Agent delegation** | `delegate_to_agent` action; sub-runner spawn; return-value plumbing | none | M |
| 9 | **Unified budget tracker** | per-call cost+tokens; shared across EscalatingLlm + ADK + loop_budget | none | S |

(S ≈ <300 LOC, M ≈ <1000 LOC, L ≈ <2500 LOC.)

## 5. Suggested sequencing

```
#1 → #2 → #3 → #4 → #5
                     ↓
#8 (parallel)     #6 → (done)
#9 (parallel)         ↓
                     #7
```

Rationale:
- #1 first: highest Doer ROI, no deps, scaffolds the `tools/` package others reuse.
- #2 unlocks web tasks and is prerequisite for #6.
- #3 reuses the session abstraction from #1.
- #4 must precede #5 (microagents inject into the condensed event stream).
- #6 piggybacks on #2's screenshot output.
- #7 wraps both #1 (bash) and #3 (ipython) in Docker.
- #8 and #9 are orthogonal — can land at any point.

## 6. Cross-cutting principles (enforced across all subs)

- **KISS.** No new heavy deps unless a feature truly demands it. Prefer system binaries (tmux, ripgrep), files on disk, single ADK FunctionTool entry points.
- **Separation of concerns.** Each new capability lives in its own module under `aiforge_core/runtime/tools/` (or `runtime/memory/` for #4-#5). Sibling tool modules do not import each other.
- **Defense-in-depth.** ADK per-agent `tools=[...]` filter + GA `tool_before_callback` reject + harness trace assertion. Same model as today.
- **Soft-error contract.** Every tool returns `{ok, ...}` dict; never raises into the model loop.
- **ADK ≥2.0.0b1.** Pin already in `pyproject.toml`; verified installed.
- **`agents.yaml` is source of truth.** Tool allowlists, sub-command allowlists, memory scope, termination contracts — all declared per agent.
- **Trace everything new.** Each new capability adds a labeled Neo4j event node (`:Think`, `:Finish`, `:BashSession`, `:EditorUndo`, `:Browse`, `:Cell`, `:Delegate`, `:Cost`). Indexed and pruned by existing observability TTL.
- **Test before integrate.** Unit tests in `tests/python/runtime/...`; integration smoke test booting ADK; regression suite re-running ONE-107/108/109 fixtures.

## 7. Out of scope (for the whole roadmap)

- Replacing the ADK orchestration spine with a homegrown loop.
- Replacing AiForgeMemory with OpenHands' event-stream condenser (we keep AiForgeMemory and bolt a condenser on top).
- Migrating away from LM Studio / EscalatingLlm provider routing.
- Replacing the smolagents GA fallback for the Doer.
- Adding GUI/web frontend beyond current `web/` dir.

## 8. Open questions

| # | Question | Default if unanswered |
|---|---|---|
| Q1 | Should Docker sandbox (#7) be opt-in per agent, or default-on once shipped? | opt-in via `runtime: adk_agent_with_ga_docker` |
| Q2 | Does microagent (#5) trigger matching run on Doer turns only, or also Planner/Researcher? | Doer-only; expand once empirical |
| Q3 | Vision (#6) — accept Claude Vision via Architect, or also local MLX vision models? | Architect/cloud only initially |
| Q4 | Cost tracker (#9) — store per-call or per-turn? | per-call; rolled up per-turn in views |

## 9. Decision log

- **2026-05-21:** decomposed into 9 subs (this doc).
- **2026-05-21:** sub #1 specced separately (`2026-05-21-tool-surface-upgrade-design.md`); user approved approach B (layered tools package).
- **2026-05-21:** ADK migration audit closed — already on `2.0.0b1`.
