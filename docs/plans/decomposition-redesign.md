# Decomposition + Execution Redesign

Status: PROPOSAL (awaiting confirmation). Owner: pipeline.

## Problem (what the user hit)
1. Pipeline is task-type **blind** — routed on complexity only, so a "analyze 3
   repos → Confluence page" ran the CODE pipeline (tests+python+md). *(partly
   fixed: ab61daa routes doc/analysis → single agent — but see #4, that now
   over-blocks multi-repo analysis.)*
2. Analysis over **many repos / topics** does not fan out into independent
   subtasks — the split logic only decomposes CODE by file.
3. A **big code task** should split into smaller sub-tasks and run in **one
   shared git worktree**, sequential or parallel decided by a **planner** (by
   dependency, not a fixed file count).
4. When **each subtask is itself big**, it must **recursively** spin off its
   own sub-agents (sequential + parallel), and every level needs **tightening**
   (bounds, disjoint-file enforcement, fresh scoped context, verification).
5. CodeGraph is never built for a pinned folder and never fed to the LLM
   *(decided: blocking first-time `codegraph init` + tool-only, gate cwd fix)*.

## Target model

```
turn → CLASSIFY(task_type) ─┬─ simple            → single agent
                            ├─ doc / single-topic → single research agent (draft)
                            ├─ analysis (multi)   → ANALYSIS FAN-OUT
                            └─ code build (big)   → CODE FAN-OUT
```

### Classifier
- `simple`  : small / single-file / chit-chat.
- `doc`     : confluence/report/summary, single deliverable.
- `analysis`: read/explore; **fan-out iff** ≥2 repos OR ≥2 explicit topics,
  else single research agent. (Narrows ab61daa.)
- `code`    : multi-file build.

### Decomposers
- **Code**: architect → file/module subtasks, each with `deps: [ids]` +
  `files: [globs]` (disjointness known up front).
- **Analysis**: by **repo** (topics explored within each repo) → one explore
  subtask per repo, `read-only`.

### Scheduler (planner-driven, shared worktree)
- One git worktree per top-level run (NOT one-per-subtask + merge).
- Build a DAG from `deps`. Run a wave of subtasks whose deps are satisfied;
  within a wave, **parallel iff files disjoint**, else serialize the overlap.
- Cap concurrency `AIFORGE_PARALLEL_SUBTASKS_MAX`.

### Recursive decomposition (the new part)
- A subtask returns `too_big` (budget exhausted / touches many files) →
  the executor **re-decomposes it** one level deeper into sub-agents and runs
  them under the SAME scheduler (seq/parallel by their deps/disjointness).
- Depth cap (`AIFORGE_DECOMP_MAX_DEPTH`, default 2) to prevent runaway.
- Each sub-agent gets a **fresh scoped context** (only its goal + shared spec),
  hard file scope, and the tighten rules below.

### Analysis executor (read-only, SAFE)
- Explore subtasks run in the **real repo dir** → must be **read-only**
  (role=`researcher`: file_read/grep/repo_map/codegraph/web; file_write/patch/
  bash forbidden). No worktree needed.
- Parallel across repos → each yields a findings.md.
- **Synthesize** stage: merge all findings into the deliverable (draft report /
  Confluence markdown). Draft-only (no publish unless user says publish/post).

### Tightening (every sub-agent, all levels)
- Fresh context = goal + shared SPEC only (no sibling history).
- Hard file scope (`scope_allowlist_globs`); writes outside scope rejected.
- Disjoint-file enforcement before a parallel wave.
- Per-subtask verify (compile+test for code; findings-non-empty for analysis).
- Bounded turns/wall; `too_big` → recurse (once) or core-first retry.

## Phased build order
- **P0** Narrow ab61daa: analysis fan-out vs single-agent split in the router.
- **P1** Analysis fan-out pipeline (by-repo explore → synthesize → draft).
  Highest user value, lowest risk (read-only, new module).
- **P2** Shared-worktree scheduler with dep-DAG + disjoint-parallel waves
  (replaces per-subtask-worktree+merge for code).
- **P3** Recursive decomposition (`too_big` → one level deeper) + depth cap.
- **P4** CodeGraph: blocking first-time `codegraph init` + gate cwd fix +
  ensure tools advertised.
- **P5** Tightening pass across all levels (scope, verify, bounds).

## Already shipped this effort
- Web: certifi TLS, web_crawl+web_search to all agents, SSRF guard, gate on.
- Reject → stop+wait (all modes).
- Chat protocol-leak (`ARGS_JSON: null`) + null-arg coercion.
- Doc/analysis routing (ab61daa) — to be narrowed in P0.

## Open assumptions to confirm
- Recursion depth default = 2 (subtask → sub-agents → stop).
- Shared worktree replaces per-subtask worktrees for code (no merge step).
- Analysis explore = read-only in real repo dirs (no writes, no worktree).
