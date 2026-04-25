# Context Strategy Eval — Z vs A vs B

**Date:** 2026-04-25
**Owner:** Architect (Claude Code) + user
**Goal:** Decide whether to keep Neo4j `graph_rag` as primary code-context source, replace it with Aider-style tree-sitter repo-map, or run pure-agentic (ripgrep + read + sub-agent).

## Context

User pain on ONE-51..ONE-54: agents repeatedly read same files, blow context, miss relationships, give up. OSS-agent survey (see `memory/project_code_indexing_research.md`) found nobody runs Neo4j-style runtime graph. Field has split into two dominant patterns:

- **Pure agentic** (opencode / Cline / Claude Code): ripgrep + glob + read on demand, with explorer sub-agent that returns summaries.
- **Tree-sitter symbol map** (Aider): in-memory graph + PageRank-ranked signature digest injected into system prompt every turn.

This eval picks a winner empirically against two AIForgeCrew fixtures.

## Tracks

| ID | Strategy | Doer toolset | Extra context source |
|----|----------|--------------|----------------------|
| **Z** | Status quo (control) | read_file, edit_block, write_file, run_compile, grep, list_dir, **+ graph_rag MCP (25 tools)** | Neo4j graph (current) |
| **A** | Pure agentic | read_file, edit_block, write_file, run_compile, grep, list_dir, **+ ask_explorer** | Explorer sub-agent (mlx-planner reuse) |
| **B** | Aider repo-map | read_file, edit_block, write_file, run_compile, grep, list_dir, **+ graph_rag MCP** | Neo4j (kept, watch usage) **+ repo-map digest in system prompt (~1.5k toks)** |

`graph_rag` toggle = `AIFORGE_GRAPH_MCP_ENABLED` env (already exists in `aiforge_core/doer/tools.py:548`). Repo-map injection is a new branch in `aiforge_core/doer/agent.py`.

## Fixtures

Fresh tickets created by the harness per run (no replay; tickets are stateful in Postgres).

### F1 — Single-file logger add (small)
- **Repo:** PosClientBackend
- **Title:** "Add request-correlation logging to PosController"
- **Body:** Inject `MDC.put("requestId", ...)` at controller entry + `clear()` at exit. Existing pattern in `BusinessController.java`. Single file change, single class, ~15 LoC.
- **Why:** Tests if context strategy matters at all on trivial tickets. Expect Z≈A≈B.

### F3 — Multi-class Java refactor (medium, compile-gated)
- **Repo:** PosClientBackend
- **Title:** "Extract `ProductPriceCalculator` from `BusinessProductsController`"
- **Body:** Pull pricing logic (3 methods, ~80 LoC) out of `BusinessProductsController.java` into new `service/ProductPriceCalculator.java`, wire via constructor injection, update 2 call sites, keep behavior identical, all existing tests must pass.
- **Why:** Tests cross-file relationship awareness. Discriminating between strategies expected.

(F2 ONE-53, F4 ONE-9-style deferred to follow-up if Z+A+B inconclusive.)

## Metrics (per run)

| Metric | Source | Format |
|--------|--------|--------|
| `compile_pass` | `run_compile` exit / counters.compile_green | bool |
| `total_tokens` | sum of `usage.total_tokens` across `llm.call` events | int |
| `prompt_tokens` | sum of `usage.prompt_tokens` | int |
| `completion_tokens` | sum of `usage.completion_tokens` | int |
| `steps` | count of smolagents Step headers | int |
| `wall_clock_s` | first→last event ts | float |
| `read_file_calls` | tool_call name=read_file count | int |
| `read_file_dedup_hits` | "stub" returns from read_file | int |
| `graph_rag_calls` | tool_call name in graph_rag allowlist | int |
| `grep_calls` | tool_call name=grep count | int |
| `final_answer` | Doer reached final_answer | bool |
| `tool_call_distribution` | name→count | dict |

Captured per-run JSON to `evals/results/{track}/{fixture}/{seq}.json`.

## Run protocol

For each (track, fixture) pair, run **3 times**.

1. Reset graph-runner: stop, clear `~/.aiforge/logs/graph-runner.{out,err}`, restart with track-specific env:
   - **Z**: `AIFORGE_GRAPH_MCP_ENABLED=1` (default)
   - **A**: `AIFORGE_GRAPH_MCP_ENABLED=0 AIFORGE_EXPLORER_ENABLED=1`
   - **B**: `AIFORGE_GRAPH_MCP_ENABLED=1 AIFORGE_REPO_MAP_ENABLED=1`
2. POST `/api/tickets` with fixture body (clean ticket identifier per run).
3. Tail `/api/llm-trace/{id}/stream` until ticket reaches DONE/ERROR or timeout (10 min).
4. Collect trace events + final ticket state + worktree git diff.
5. Compute metrics, write JSON.

Aborts on infra fail (LM Studio OOM, NATS down, etc.) — re-run that single sample, don't skip.

## Decision rule

Min path = Z + A + B on F1 + F3 = 18 runs (~3 hr wall-clock).

| Outcome | Ship |
|---------|------|
| A wins (≥80% pass at lower tokens than Z) | Track A — kill Neo4j as Doer context source |
| B wins (≥80% pass + sees `graph_rag_calls` drop to ~0) | Track B — keep Neo4j but demote to cold lookup, repo-map is primary |
| Z wins (current is best) | Stop. Investigate whether the pain is elsewhere (model? prompt?) |
| A ≈ B > Z | Run Track C (hybrid) on F3 only; otherwise pick by ops simplicity → **A** |

## Implementation order

1. Plan doc (this file). ✅
2. Eval harness skeleton (`scripts/evals/run_eval.py`):
   - `--track Z|A|B`, `--fixture F1|F3`, `--samples N`
   - Fixture loader from `evals/fixtures/{F1,F3}.yaml`
   - POST ticket, stream trace, parse metrics, write JSON
3. EVAL-Z baseline run (F1+F3 × 3). No code changes needed — just toggle env.
4. EVAL-A: build explorer sub-agent (reuse mlx-planner @ :1235), wire `ask_explorer` tool. Toggle `AIFORGE_EXPLORER_ENABLED`. Run F1+F3 × 3.
5. EVAL-B: build `aiforge_core/repo_map/` (tree-sitter + NetworkX PageRank + token-budget renderer). Inject into Doer system prompt when `AIFORGE_REPO_MAP_ENABLED=1`. Run F1+F3 × 3.
6. Compare report. Telegram user.

## Risks / open questions

- **Run-to-run variance**: 3 samples may not separate signal from noise. Bump to 5 if results are within ~15% on any axis.
- **mlx-planner contention** (Track A): explorer reuses planner; if planner is busy for the same ticket, explorer waits. Acceptable — measures real-world latency.
- **Repo-map cold start** (Track B): tree-sitter parse of PosClientBackend (~500 Java files) is one-time cost. Budget 30s on first run, subsequent runs use cache.
- **Fixture leakage**: F1 and F3 will be in git history after first run; agent prompt should not mention prior runs. Worktrees are fresh per run, so no contamination.
