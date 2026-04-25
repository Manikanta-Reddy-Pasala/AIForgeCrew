# AIForgeCrew v5 Migration Plan

**Status:** Draft
**Author:** [ ]
**Date:** 2026-04-25
**Total estimate:** ~28 days, 1 dev (some phases parallelizable)

---

## Context

### Current state (v4)

- **Orchestration:** LangGraph state machine (`supervisor → planner → doer → feedback → learner`)
- **Agents:** smolagents `ToolCallingAgent` (Doer), `CodeAgent` (Planner)
- **Models:** `mlx_lm.server :1234` (Doer), `mlx_lm.server :1235` (Planner) on Mac Studio (MS)
- **Postgres on NUC:** tickets, memories, checkpoints — already deployed
- **Neo4j 5.26 Community on NUC** — already deployed (currently used only by `graph_rag` MCP)
- **Hindsight pgvector:** facts memory (legacy)
- **Filesystem GA memory:** `~/genericagent/memory/` (used when GenericAgent runs standalone)
- **Eval signal:** F1–F7 + chain proven on text-protocol GA Doer; planner-driven decomposition unlocks 5-file tasks

### v5 target

- **ADK orchestration** replaces LangGraph
- **GenericAgent (GA) embedded inside ADK `BaseAgent`** for Doer (and conditionally Planner per EVAL-1b)
- **Aider RepoMap** as hot-path code digest
- **Graphify** nightly job for full code map
- **Neo4j unified memory**, 5 layers:
  - L0 META — meta-rules / governance
  - L1 — ADK session (in-process, not Neo4j)
  - L2 — facts (replaces hindsight pgvector)
  - L3 — SOPs
  - L4 — sessions / turns
  - L5 — code AST (files / symbols / calls)
- **`agents.yaml`-driven** per-agent rules, allowed/forbidden tools, model bindings
- **No backwards compat** — git is the rollback path

---

## Phase 1 — Lock contracts (2 days)

Goal: pin agent contracts and rule-checking before any architectural change. This is the foundation everything else relies on.

- [ ] Owner: [ ]
  Write `aiforge_core/agents.yaml` (separate agent will produce this — **dep: external authoring task**)
  - Per agent: `role`, `model`, `endpoint`, `temperature`, `tools_allowed`, `tools_forbidden`, `max_steps`, `system_prompt_ref`, `escalation_target`
  - Roles: `planner`, `doer`, `feedback`, `learner` (+ optional `architect`)
  - Est: 0.25 d (received as artifact)
  - Acceptance: file parses, `pydantic` schema validates, all v4 roles covered
- [ ] Owner: [ ]
  Write `docs/agent-rules.md` describing the rule contract & enforcement layer
  - Sections: rule taxonomy (allow/forbid/budget), enforcement points (pre-tool, post-event, end-of-turn), violation severity levels
  - Cross-link from `docs/architecture.md`
  - Est: 0.5 d
  - Acceptance: doc reviewed, links from architecture.md
- [ ] Owner: [ ]
  Add `aiforge_core/agents.py` w/ loader + validator
  - `load_agents() -> dict[str, AgentSpec]`
  - Validate against `pydantic` model
  - CLI: `python -m aiforge_core.agents validate` exits non-zero on errors
  - Est: 0.5 d
  - Acceptance: `pytest tests/test_agents_loader.py` green, CLI returns 0 on valid file
- [ ] Owner: [ ]
  Add harness rule-checker that asserts trace events match `agents.yaml` allowed/forbidden lists
  - Hook into existing `llm_trace` event stream
  - On violation: emit `RuleViolation` event, optionally fail the run (env flag `AIFORGE_RULES_STRICT=1`)
  - Est: 0.75 d
  - Acceptance: synthetic trace with forbidden tool call → checker raises; clean trace passes; F1 baseline trace passes

**Phase 1 acceptance:** all four checkboxes done, CI green, `agents.yaml` is the single source of truth for agent contracts.

---

## Phase 2 — Aider RepoMap on hot path (2 days)

Goal: prove RepoMap as hot-path digest before swapping orchestration. Reversible behind env flag.

- [ ] Owner: [ ]
  `uv tool install aider-chat` on Mac Studio
  - Pin version in `scripts/runtime/install-aider.sh`
  - Est: 0.25 d
  - Acceptance: `aider --version` resolves, version pinned in script
- [ ] Owner: [ ]
  Write `aiforge_core/index/aider_map.py` wrapping `aider.repomap.RepoMap`
  - Function: `build_digest(repo_root, focus_files, token_budget=4000) -> str`
  - Cache by `(repo_root, git_head, focus_tuple)` in-process
  - Est: 0.75 d
  - Acceptance: unit test produces non-empty digest within budget on AIForgeCrew itself
- [ ] Owner: [ ]
  Inject digest into existing Doer system prompt behind env flag `AIFORGE_AIDER_REPOMAP_ENABLED=1`
  - Prepend a `<repo-map>...</repo-map>` block; truncate to budget
  - Log digest length + cache hit/miss in trace
  - Est: 0.5 d
  - Acceptance: with flag on, trace event `repomap.injected` fires; with flag off, no change
- [ ] Owner: [ ]
  Re-run F1–F7 chain → must match X2 baseline (digest doesn't regress)
  - Compare token count, pass rate, step count
  - Est: 0.5 d
  - Acceptance: pass rate ≥ X2 baseline; tokens within +/-15%; trace shows digest visible

**Phase 2 acceptance:** F1–F7 chain green with flag on, digest visible in trace, perf within tolerance.

---

## Phase 3 — Tree-sitter + Neo4j schema (3 days)

Goal: stand up Neo4j as the unified memory backbone. Schema first, ingest second, query template third.

- [ ] Owner: [ ]
  Confirm Neo4j ≥5.11 (audit: NUC running 5.26 Community — **DONE**)
  - Note GDS plugin status; vector index needs ≥5.11
  - Est: 0.1 d
  - Acceptance: `CALL dbms.components()` returns 5.26+
- [ ] Owner: [ ]
  Schema migration: constraints + indexes
  - Constraints: unique `(:File {path})`, `(:Symbol {qname})`, `(:Fact {id})`, `(:Sop {id})`, `(:Session {id})`, `(:Turn {id})`
  - Vector index on `:Fact.embedding` (dim TBD from hindsight check)
  - Fulltext index on `:Fact.text`, `:Sop.text`
  - Est: 0.5 d
  - Acceptance: Cypher `SHOW INDEXES` lists all expected indexes
- [ ] Owner: [ ]
  Write `aiforge_core/index/treesitter.py` for Java/Python/TS ingest → `:File` / `:Symbol` / `:CALLS`
  - Use `tree-sitter` Python bindings + grammars
  - Output: per-file batched MERGE Cypher
  - Est: 1 d
  - Acceptance: ingest produces non-zero `:File` and `:Symbol` counts on AIForgeCrew repo
- [ ] Owner: [ ]
  One-time ingest of `PosClientBackend`, `AIForgeCrew`, `oneshell-commons`
  - Script: `scripts/index/ingest-treesitter.py`
  - Tag nodes with `repo` property for cross-repo queries
  - Est: 0.5 d
  - Acceptance: Neo4j shows `:File` count within ±1% of `find -name '*.{java,py,ts,tsx}' | wc -l` per repo
- [ ] Owner: [ ]
  Cypher template `graph_lookup(template, params)` w/ hybrid ranking
  - Combine vector similarity (Fact.embedding) + fulltext score + graph proximity
  - Templates: `find_symbol_def`, `find_callers`, `fact_search`, `sop_search`
  - Expose as Python helper in `aiforge_core/memory/graph_lookup.py`
  - Est: 0.75 d
  - Acceptance: returns top-K facts/symbols for a sample query in <100ms (warm cache)

**Phase 3 acceptance:** Neo4j has constraints + indexes, three repos ingested, `graph_lookup` returns relevant top-K under 100ms.

---

## Phase 4 — Graphify nightly + mirror to Neo4j (2 days)

Goal: layered code map. RepoMap = hot path; Graphify = cold/full map mirrored into Neo4j L5.

- [ ] Owner: [ ]
  Confirm `graphifyy` already installed on MS (audit: **YES**)
  - Document version + binary path in `docs/runbook.md`
  - Est: 0.1 d
  - Acceptance: `graphify --version` resolves on MS
- [ ] Owner: [ ]
  Cron on NUC: `graphify clone <repo>` for each known repo
  - Repos: `PosClientBackend`, `PosServerBackend`, `oneshell-commons`, `MongoDbService`, `AIForgeCrew`
  - Run off-peak (02:00 NUC local)
  - Est: 0.5 d
  - Acceptance: systemd timer fires, output `graph.json` exists per repo
- [ ] Owner: [ ]
  Write loader: `graph.json` → Neo4j `:File`/`:Symbol`/`:CALLS` mirror
  - Conflict resolution: prefer Graphify when newer than tree-sitter mirror; preserve tree-sitter `:Symbol` if Graphify missing it
  - Tag `provenance: graphify | treesitter`
  - Est: 0.75 d
  - Acceptance: post-load `:File` count matches Graphify `graph.json` ±0.5%
- [ ] Owner: [ ]
  Cross-repo: `graphify merge-graphs` step before mirror
  - Merge per-repo `graph.json` into `merged.json`
  - Resolve fully-qualified-name collisions
  - Est: 0.4 d
  - Acceptance: cross-repo `:CALLS` edges exist (e.g. `BusinessService` → `oneshell-commons` symbol)
- [ ] Owner: [ ]
  Acceptance test: nightly job produces fresh `:File` count matching `find -name *.java | wc -l` ±1%
  - Add `scripts/index/verify-graphify-mirror.sh`
  - Est: 0.25 d
  - Acceptance: verify script exits 0 after a fresh nightly run

**Phase 4 acceptance:** nightly cron emits Graphify output, mirror loader populates Neo4j L5, cross-repo edges visible, file count within tolerance.

---

## Phase 5 — L0/L2/L3/L4 memory migration (3 days)

Goal: unify all memory in Neo4j (except L1 = ADK in-session). GA standalone keeps working via reverse-export.

- [ ] Owner: [ ]
  Migrate hindsight pgvector → `:Fact {source: aiforge_legacy}`
  - Preserve embeddings if dim matches Neo4j vector index dim
  - **Risk:** dim mismatch — see Risk register #3
  - Est: 0.5 d
  - Acceptance: `MATCH (f:Fact {source: 'aiforge_legacy'}) RETURN count(f)` matches hindsight row count
- [ ] Owner: [ ]
  Wire `learner_node` to write `:Fact` directly post-success
  - New helper: `aiforge_core/memory/fact_writer.py`
  - Embed text via existing embed sidecar (until removed in Phase 11)
  - Est: 0.5 d
  - Acceptance: a successful F1 run produces ≥1 new `:Fact` node
- [ ] Owner: [ ]
  Add `:Session` / `:Turn` schema; add ADK `BasePlugin.on_event_callback` mirror
  - `:Session {id, ticket_id, started_at, status}`
  - `:Turn {id, session_id, role, content, tool_calls, started_at, ended_at}`
  - Plugin lives in `aiforge_core/runtime/neo4j_mirror_plugin.py` (also referenced in Phase 6)
  - Est: 0.5 d
  - Acceptance: synthetic ADK run produces `:Session` + N `:Turn` nodes
- [ ] Owner: [ ]
  Migrate GA filesystem memory:
  - `~/genericagent/memory/global_mem.txt` + `global_mem_insight.txt` → `:Fact {source: ga_l2}`
  - `~/genericagent/memory/*_sop.md` → `:Sop {kind, applies_to_role}`
  - `~/genericagent/memory/memory_management_sop.md` → `:MetaSop {kind: memory_mgmt}`
  - `~/genericagent/memory/L4_raw_sessions/*` → parse into `:Session` / `:Turn`
  - Script: `scripts/memory/migrate-ga-fs.py`
  - Est: 0.75 d
  - Acceptance: counts match per source category; spot-check 5 facts retrievable
- [ ] Owner: [ ]
  Markdown ingest cron (NUC systemd): `~/.claude/memory/*.md` → `:Fact {source: claude_md}` daily
  - Idempotent: hash `(path, mtime)` to skip unchanged
  - Est: 0.4 d
  - Acceptance: timer logs successful run; second run logs zero new facts on unchanged files
- [ ] Owner: [ ]
  Reverse-export cron: regenerate `~/genericagent/memory/global_mem_insight.txt` from Neo4j nightly
  - Keeps GA standalone usable for ad-hoc work
  - Script: `scripts/memory/export-ga-l2.py`
  - Est: 0.35 d
  - Acceptance: GA standalone session loads memory file without errors after cron run

**Phase 5 acceptance:** 100+ facts in Neo4j, search returns relevant top-K, GA standalone still loads memory.

---

## Phase 6 — GA Doer in ADK (5 days, **long pole**)

Goal: replace smolagents Doer with GA-inside-ADK. Riskiest phase — done after L2/L3/L5 are functional so the new Doer has its memory.

- [ ] Owner: [ ]
  `pip install google-adk` in AIForgeCrew venv
  - Pin version in `pyproject.toml`
  - Est: 0.1 d
  - Acceptance: `python -c "import google.adk"` succeeds
- [ ] Owner: [ ]
  Write `aiforge_core/runtime/adk_agent.py`:
  - `class GenericAdkAgent(BaseAgent)` — `_run_async_impl` calls `agent_runner_loop` from GA
  - `class AiForgeHandler(GenericAgentHandler)` — overrides:
    - `_get_abs_path` → ScopeGuard
    - `tool_before_callback` → forbidden-tool check (uses `agents.yaml`)
    - `turn_end_callback` → Neo4j `:Turn` mirror
    - `do_start_long_term_update` → replace with no-op (handled by Phase 5 learner)
    - adds `do_ask_explorer` → calls existing explorer sub-agent
  - Monkey-patch `ga.get_global_memory` → Neo4j L2/L3/L5 loader (uses `graph_lookup` from Phase 3)
  - Est: 2 d
  - Acceptance: unit test shows `GenericAdkAgent` runs a trivial ticket end-to-end against a mock LLM
- [ ] Owner: [ ]
  Write `aiforge_core/runtime/neo4j_mirror_plugin.py` — ADK `BasePlugin` that writes `:Turn` per event
  - Subscribe to `model_response`, `tool_call`, `tool_result`, `agent_end`
  - Batch-write per turn (not per event) to amortize Neo4j round-trip
  - Est: 0.75 d
  - Acceptance: integration test: 1 ticket run → expected `:Turn` count in Neo4j
- [ ] Owner: [ ]
  Write `aiforge_core/runtime/role_tool_schemas.py` — per-agent `tools_schema` slice
  - Reads `agents.yaml`, slices the global tool registry per role
  - Est: 0.4 d
  - Acceptance: `tools_schema(role='doer')` returns subset matching `tools_allowed` in `agents.yaml`
- [ ] Owner: [ ]
  Replace LangGraph `doer_node` with `GenericAdkAgent(role='doer')` behind feature flag `AIFORGE_ADK_DOER_ENABLED=1`
  - Wrapper in `aiforge_core/graph/nodes/doer.py` chooses path based on flag
  - Est: 0.5 d
  - Acceptance: with flag on, Doer trace shows ADK events; with flag off, smolagents path unchanged
- [ ] Owner: [ ]
  Re-run F1–F7 + chain v4 on new path → must match or beat X2 baseline
  - Compare: pass rate, step count, token count, wall time
  - Est: 1 d
  - Acceptance: F7 chain v4 passes 3/3 via ADK+GA, OTel traces flow, X2 baseline matched or beaten on ≥4/7 fixtures

**Phase 6 acceptance:** F7 chain v4 passes 3/3 via ADK+GA, OTel traces flow, regression-free vs X2 baseline.

---

## Phase 7 — GA Planner eval (2 days)

Goal: data-driven decision on whether to swap Planner backend. EVAL-1b decides.

- [ ] Owner: [ ]
  Run EVAL-1b: GA Planner vs CodeAgent Planner × 3 fixtures × 3 samples
  - Fixtures: F1, F3, F-multi-file
  - Metrics: plan-quality rubric (0–5), token cost, wall time, valid-output rate
  - Est: 1.5 d
  - Acceptance: results CSV in `evals/results/eval-1b/`, summary table in `evals/results/eval-1b/README.md`
- [ ] Owner: [ ]
  Decision: switch Planner to GA only if EVAL-1b shows GA wins or ties on plan quality + token cost
  - Document outcome + decision in `docs/decisions/` (new ADR file)
  - Est: 0.5 d
  - Acceptance: ADR committed, decision propagated to Phase 11 cleanup checklist

**Phase 7 acceptance:** EVAL-1b results published, decision recorded as an ADR. If GA wins: schedule Planner swap inside Phase 9. If GA loses: keep CodeAgent — see Risk #1.

---

## Phase 8 — Feedback + Learner via direct LiteLLM (1 day)

Goal: drop unnecessary scaffolding from feedback/learner — they're single-LLM-call stages.

- [ ] Owner: [ ]
  Replace LangGraph `feedback_node` with single LiteLLM call via text-protocol prompt
  - Lives in `aiforge_core/runtime/feedback.py`
  - Prompt template in `aiforge_core/prompts/feedback.txt`
  - Est: 0.4 d
  - Acceptance: F1 run still produces a structured feedback object, schema unchanged
- [ ] Owner: [ ]
  Replace `learner_node` with single LiteLLM call + ADK plugin `:Fact` write hook
  - Lives in `aiforge_core/runtime/learner.py`
  - Reuses `fact_writer` from Phase 5
  - Est: 0.4 d
  - Acceptance: F1 run produces ≥1 new `:Fact` post-success
- [ ] Owner: [ ]
  End-to-end smoke: full ticket runs cleanly through feedback + learner
  - Est: 0.2 d
  - Acceptance: F1 ticket completes; both nodes visible in trace

**Phase 8 acceptance:** end-to-end ticket runs cleanly, feedback + learner are simple LiteLLM calls.

---

## Phase 9 — LangGraph → ADK orchestration full swap (5 days)

Goal: the full orchestration handover. After this, LangGraph deps can be removed.

- [ ] Owner: [ ]
  Replace `aiforge_core/graph/graph.py` with `aiforge_core/runtime/adk_workflow.py`
  - Top-level: `SequentialAgent([planner_agent, doer_chain_agent, feedback_agent, learner_agent])`
  - `doer_chain_agent` is a `LoopAgent` over planner-emitted subtickets
  - Conditional escalation via custom `BaseAgent` reading session state
  - Est: 2 d
  - Acceptance: graph-runner `python -m aiforge_core.runtime.api ticket-id <id>` runs to completion via ADK
- [ ] Owner: [ ]
  Replace `langgraph` checkpoints with `DatabaseSessionService` (Postgres on NUC)
  - Schema migration in `migrations/`
  - Connection string from existing `AIFORGE_PG_URL`
  - Est: 1 d
  - Acceptance: a ticket interrupted mid-run resumes from the last `:Session`-backed checkpoint
- [ ] Owner: [ ]
  Wire OTel exporter to existing observability stack
  - Reuse current trace exporter config
  - Est: 0.5 d
  - Acceptance: OTel traces appear in existing dashboard for an ADK-driven ticket
- [ ] Owner: [ ]
  Remove the `AIFORGE_ADK_DOER_ENABLED` flag — ADK becomes the only path
  - Est: 0.25 d
  - Acceptance: flag references gone from code, smolagents-Doer path code-deleted (Phase 11 finishes the dep removal)
- [ ] Owner: [ ]
  Full eval pass: F1–F7 + chain v4 via ADK orchestration
  - Est: 1.25 d
  - Acceptance: pass rate matches Phase 6 result; no regression

**Phase 9 acceptance:** graph-runner serves tickets via ADK end-to-end; LangGraph deps can be removed from `pyproject.toml` (deferred to Phase 11).

---

## Phase 10 — Markdown ingest crons + cleanup (2 days)

Goal: consolidate scheduled jobs onto NUC; remove now-redundant components.

- [ ] Owner: [ ]
  Move existing MS launchd jobs (`reindex-daily`, `file-indexer`, `repo-indexer`) to NUC systemd timers per audit recommendation
  - Source units: `scripts/runtime/com.aiforge.*.plist` → new `scripts/systemd/aiforge-*.timer` + `.service`
  - Disable launchd jobs on MS via `launchctl unload`
  - Est: 1 d
  - Acceptance: NUC `systemctl list-timers | grep aiforge` shows expected timers; MS launchd shows no AIForge jobs
- [ ] Owner: [ ]
  Decommission `graph_rag` MCP (replaced by `graph_lookup` Cypher tool)
  - Remove from `lmstudio-mcp-nuc-ip.json` and prod equivalent
  - Stop+disable systemd unit
  - Est: 0.5 d
  - Acceptance: MCP server no longer listening; downstream code paths use `graph_lookup`
- [ ] Owner: [ ]
  Verify ingest crons run on NUC nightly
  - Smoke: trigger one cron manually, confirm Neo4j updates
  - Est: 0.5 d
  - Acceptance: post-run, Neo4j shows new/updated nodes; logs clean

**Phase 10 acceptance:** ingest crons run on NUC nightly, MS launchd jobs disabled, `graph_rag` MCP retired.

---

## Phase 11 — Sunset (2 days)

Goal: prune. After all evals are green, delete the old code.

- [ ] Owner: [ ]
  Remove from `pyproject.toml`:
  - `langgraph`
  - `smolagents` (Planner only — keep if EVAL-1b says CodeAgent wins; remove fully if GA wins)
  - `hindsight`
  - Est: 0.25 d
  - Acceptance: `uv sync` resolves cleanly without these deps
- [ ] Owner: [ ]
  Delete code:
  - `aiforge_core/mcp_graph.py`
  - `aiforge_core/embed.py` (legacy LM Studio embed sidecar)
  - `aiforge_core/graph/` (old LangGraph nodes)
  - Smolagents Doer adapter (kept around feature-flag in Phase 6)
  - Est: 0.5 d
  - Acceptance: `pytest` green, no stale imports
- [ ] Owner: [ ]
  Update `README.md` + `docs/architecture.md` to reflect v5 reality
  - Diagram update: remove LangGraph, add ADK + Neo4j L0–L5
  - Est: 0.5 d
  - Acceptance: doc review by self; no stale references to LangGraph / hindsight / smolagents (where removed)
- [ ] Owner: [ ]
  Final verification: full F1–F7 eval suite + 3 real tickets end-to-end
  - Est: 0.75 d
  - Acceptance: tests green, code smaller (line-count delta logged), traces flow, README updated

**Phase 11 acceptance:** tests green, deps pruned, code smaller, traces flow, docs current.

---

## Parallelization plan

These phases can run partially in parallel with one dev:

| Track | Phases | Notes |
|-------|--------|-------|
| Code-map (RepoMap + Graphify) | 2 + 4 | Phase 4 needs Phase 3's Neo4j schema, but Graphify install + cron prep is independent of RepoMap. |
| Memory (Neo4j + migration) | 3 + 5 | Schema (Phase 3) lands first; migration (Phase 5) starts as soon as constraints exist. |
| Eval | 7 | Can run anytime after Phase 6. Doesn't block Phase 8 or 9. |

**Critical path:** Phase 1 → Phase 3 → Phase 5 → Phase 6 → Phase 9 → Phase 11.

Phase 6 is the long pole and the riskiest. It's gated on Phase 5 because the new Doer expects its memory layer to exist.

---

## Total estimate

| Phase | Days | Cumulative |
|-------|------|------------|
| 1  | 2 | 2 |
| 2  | 2 | 4 |
| 3  | 3 | 7 |
| 4  | 2 | 9 |
| 5  | 3 | 12 |
| 6  | 5 | 17 |
| 7  | 2 | 19 |
| 8  | 1 | 20 |
| 9  | 5 | 25 |
| 10 | 2 | 27 |
| 11 | 2 | 29 |
| **Total (serial)** | **29** | |
| **Total with overlap (Phase 2+3, 4+5)** | **~28** | |

---

## Risk register

Top 5 risks + mitigations.

### Risk 1 — EVAL-1b regression on Planner

**Description:** GA Planner may underperform CodeAgent on plan quality or token cost. EVAL-1 (text-to-plan) already showed CodeAgent winning over ToolCalling on some shapes; no guarantee GA matches.

**Likelihood:** Medium
**Impact:** Medium (Planner sits at the front of the pipeline; bad plans cascade)
**Mitigation:**
- Phase 7 is explicitly a gate, not a foregone conclusion.
- Keep CodeAgent if data says so. Smolagents stays in deps (only Planner path; Doer path is removed regardless).
- Document the decision as an ADR so future re-evals are easy.

### Risk 2 — ADK + GA adapter complexity

**Description:** `GenericAdkAgent` + `AiForgeHandler` wraps two complex frameworks. Callback semantics, async boundaries, and state surface mismatches are hard to find without running real tickets.

**Likelihood:** High
**Impact:** High (Phase 6 is the long pole; slippage cascades into Phase 9)
**Mitigation:**
- Prototype Phase 6 on a branch before committing to main.
- Time-box: if Phase 6 exceeds 7 days (vs 5 estimate), pause and reassess scope (e.g. ship without `do_ask_explorer` first; add it later).
- Keep the smolagents Doer path under feature flag through Phase 9 so we can fall back without git revert.

### Risk 3 — Neo4j vector index dimension mismatch on hindsight migration

**Description:** Hindsight pgvector embeddings may not match the Neo4j vector index dim. Re-embedding 1k+ facts is non-trivial but cheap; mismatch detected late is the real risk.

**Likelihood:** Medium
**Impact:** Low (re-embed is straightforward; cost is wall time)
**Mitigation:**
- Phase 5 first task: query hindsight for embedding dim, compare to planned Neo4j vector index dim. Decide BEFORE creating the index.
- If mismatch: re-embed via current embed sidecar before the sidecar is removed in Phase 11.
- Have a fallback path: store embeddings as nullable, populate on read if missing.

### Risk 4 — Graphify nightly job impact on NUC load

**Description:** Cloning + indexing 5+ repos nightly on NUC competes with Postgres / Neo4j / MCP servers running there. Could spike CPU/IO and degrade tickets in flight.

**Likelihood:** Low
**Impact:** Medium (degraded ticket latency overnight)
**Mitigation:**
- Rate-limit: stagger per-repo jobs by 15 min, run between 02:00–05:00 NUC local.
- `nice -n 19` + `ionice -c idle` on the cron command.
- Monitor: alert if NUC load avg >4 during cron window. Pull jobs off NUC to MS if load proves too high.

### Risk 5 — `mlx_lm` 0.31 tool_calls bug not fixed in newer versions

**Description:** v4 settled on text-protocol because `mlx_lm.server` 0.31's tool_calls path was broken. Newer versions may or may not fix it; v5 should not assume tool_calls works.

**Likelihood:** Medium
**Impact:** Medium (forces text-protocol; affects ADK tool-binding strategy)
**Mitigation:**
- Keep text-protocol indefinitely as default. Document this in `docs/agent-rules.md`.
- Add an explicit env switch `AIFORGE_TOOL_PROTOCOL={text|native}` for future re-evaluation, but ship v5 with `text`.
- Track upstream: subscribe to `mlx_lm` releases; re-test on each minor bump but do not block migration on it.

---

## Open questions

- [ ] Final naming: `:MetaSop` vs `:Sop {kind: meta}`? (decide in Phase 5)
- [ ] Embedding model for `:Fact.embedding` post-Phase-11 (after sidecar removed)?
- [ ] Does ADK `DatabaseSessionService` schema clash with existing Postgres tables? (verify in Phase 9)
- [ ] Do we need a per-repo `agents.yaml` override or stays project-global? (Phase 1 default: project-global)

---

## Appendix A — File / module map (target state)

```
aiforge_core/
  agents.py                    # Phase 1
  agents.yaml                  # Phase 1
  index/
    aider_map.py               # Phase 2
    treesitter.py              # Phase 3
    graphify_loader.py         # Phase 4
  memory/
    graph_lookup.py            # Phase 3
    fact_writer.py             # Phase 5
  runtime/
    adk_agent.py               # Phase 6
    adk_workflow.py            # Phase 9
    neo4j_mirror_plugin.py     # Phase 5/6
    role_tool_schemas.py       # Phase 6
    feedback.py                # Phase 8
    learner.py                 # Phase 8
    api.py                     # existing, kept
  graph/                       # DELETED Phase 11
  mcp_graph.py                 # DELETED Phase 11
  embed.py                     # DELETED Phase 11

scripts/
  index/
    ingest-treesitter.py       # Phase 3
    verify-graphify-mirror.sh  # Phase 4
  memory/
    migrate-ga-fs.py           # Phase 5
    export-ga-l2.py            # Phase 5
  systemd/
    aiforge-graphify.timer     # Phase 4
    aiforge-graphify.service   # Phase 4
    aiforge-md-ingest.timer    # Phase 5
    aiforge-md-ingest.service  # Phase 5
    aiforge-ga-export.timer    # Phase 5
    aiforge-ga-export.service  # Phase 5
    aiforge-reindex.timer      # Phase 10 (moved from MS)
    aiforge-reindex.service    # Phase 10 (moved from MS)

docs/
  v5-migration-plan.md         # this file
  agent-rules.md               # Phase 1
  decisions/                   # Phase 7 ADR + future
```

---

## Appendix B — Eval gates summary

| Phase | Eval gate | Pass condition |
|-------|-----------|----------------|
| 2 | F1–F7 chain w/ RepoMap on | ≥ X2 baseline pass rate, tokens within ±15% |
| 3 | `graph_lookup` micro-bench | <100ms warm cache top-K |
| 4 | Graphify mirror count | `:File` count vs `find` ±1% |
| 5 | Memory migration | 100+ facts in Neo4j, GA standalone still loads |
| 6 | F7 chain v4 via ADK+GA | 3/3 pass; ≥4/7 fixtures match-or-beat X2 |
| 7 | EVAL-1b Planner A/B | Statistical decision documented as ADR |
| 9 | Full F1–F7 via ADK orchestration | Match Phase 6 result; no regression |
| 11 | Final verification | All evals green + 3 real tickets end-to-end |
