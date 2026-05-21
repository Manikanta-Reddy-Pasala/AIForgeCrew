# OpenHands Parity Extension — Subs #11..#17

**Date:** 2026-05-22
**Depends on:** subs #1..#10 (all landed)

## Goal

Close the second-pass gaps surfaced after the 9-sub batch shipped. Memory
search (`project_eval4_mcp_toolcollection_2026-04-23`,
`project_oneshell_mcp_2026-04-24`) showed an MCP integration follow-up
that was never executed despite 8 oneshell-mcp servers running on NUC —
the highest-ROI remaining item. Six more orthogonal gaps batched alongside.

## Subs

| # | Sub | Module | One-liner |
|---|---|---|---|
| 11 | MCP client | `runtime/tools/mcp_client.py` | JSON-RPC HTTP client for FastMCP; defaults oneshell-mcp QA |
| 12 | AgentSkills | `runtime/tools/agentskills.py` | OH-style helpers auto-injected into IPython kernel |
| 13 | Truncation marker | `runtime/tools/truncation.py` | `<truncated bytes_dropped=N>` suffix |
| 14 | :Condensation event | `runtime/condensers.py` | trace event when condense() actually compacts |
| 15 | Trajectory JSON | `runtime/trajectory.py` | dump session events + state to disk for replay |
| 16 | Delegation depth cap | `runtime/tools/delegation.py` | `AIFORGE_DELEGATION_MAX_DEPTH` (default 3) |
| 17 | Repo microagents | `runtime/microagents.py` | `type: repo` files load without triggers, always inject |

## Tests

29 new unit tests:
- sub #11: 11 (`tests/python/runtime/tools/test_mcp_client.py`)
- sub #12: 4 (`tests/python/runtime/tools/test_agentskills.py`)
- sub #13: 4 (`tests/python/runtime/tools/test_truncation.py`)
- sub #14: covered by existing `test_condensers.py` paths (trace emit verified inline)
- sub #15: 6 (`tests/python/runtime/test_trajectory.py`)
- sub #16: 2 new (`test_delegation.py`)
- sub #17: 2 new (`test_microagents.py`)

Total suite: **526 passed**, 14 skipped (optional infra unchanged).

## Acceptance

1. `mcp("list_tools", endpoint="oneshell-mongo")` reaches NUC FastMCP and
   returns a non-empty tool list when both ends are live.
2. `execute_ipython_cell("open_file('hello.py', 1, 20)")` returns the
   expected window without the model writing the helper itself.
3. Truncation marker visible to the model whenever any tool caps its output.
4. `:Condensation` Neo4j nodes appear after a long-running ticket when
   `AIFORGE_CONDENSER_STRATEGY=amortized` is set.
5. `~/.aiforge/trajectories/<ticket>/<run>.json` exists after every run
   when `AIFORGE_TRAJECTORY_DUMP=1` (default).
6. Recursive `delegate_to_agent` calls beyond depth 3 return
   `delegation_depth_exceeded` without spawning a new ADK runner.
7. A `type: repo` microagent under `~/.aiforge/microagents/conv.md`
   appears in EVERY Doer prompt regardless of ticket keywords.

## Genuinely deferred (no near-term value)

| Item | Why deferred |
|---|---|
| E2BRuntime / KubernetesRuntime | Docker sandbox (sub #7) already covers contained exec |
| VSCode plugin | `web/` frontend is the supported UI surface |
| Parallel multi-tool-call dispatch | ADK 2.0.0b1 handles this natively |
| Full Action/Observation typed schema | dict-based tool returns are deliberately less rigid; equivalent info captured by `:ToolCall` + `:ToolResult` trace events |

## Verification log

- **2026-05-22:** all 7 subs landed in commit 91556b6. `pytest tests/python/ -q`
  reports 526 passed, 14 skipped (tmux/jupyter_client/aiforge DB absent
  on dev box). README + agent-rules.md + roadmap synced. NUC install
  still deferred — neither 192.168.70.115 nor 192.168.70.191 reachable
  from current network.
