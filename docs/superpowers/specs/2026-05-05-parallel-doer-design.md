# Parallel Doer Design (LM Studio 4-way concurrency)

## Why

Both `qwen/qwen3-coder-next` and `granite-4.1-30b-mxfp8` accept ≥4 concurrent
requests per LM Studio process. Today's orchestrator iterates write steps
**sequentially** in `aiforge_core/orchestrator/run_ticket.py:411`
(`for st in write_steps:`). For the 10-file Javadoc benchmark this meant
QWEN = 484s (10 steps × ~48s each). With 4-way parallel generation the
wall would drop to ~3 batches × ~50s = **~150s (3.2× speedup)**.

## Constraint: only **generation** parallelizes — apply is sequential

Doer's per-step work has two phases:
1. **Generate** — LLM call producing a unified diff for the step (slow, ~48s on qwen).
2. **Apply** — `git apply` + `git commit` to `aiforge/<ticket_id>` (fast, <1s).

If two threads call `git apply` on the same worktree concurrently we get
index lock fights and ordering loss. So:

- **Generation** runs concurrent, batch-of-N (env: `AIFORGE_DOER_PARALLEL_N`, default 4).
- **Apply** runs serial in the original step order, after each batch returns.

## Step independence — when is it safe to batch?

Use the `depends_on` field already present on each plan step. Build a graph,
run a topological sort, batch every "ready" set together. Steps with no
in-edges go into batch 1; their dependents into batch 2; etc.

For the Javadoc 10-file ticket every step targets a *different* file with
empty `depends_on` → all 10 steps in batch 1 → 3 sub-batches of 4/4/2.

## Minimal patch sketch

```python
# in run_ticket.py, replace the `for st in write_steps:` loop with:
from concurrent.futures import ThreadPoolExecutor, as_completed

parallel_n = max(1, int(_os.environ.get("AIFORGE_DOER_PARALLEL_N", "1")))
ready_batches = _topo_batches(write_steps)  # NEW helper
for batch in ready_batches:
    # Generate udiffs concurrently (LLM-bound).
    udiffs: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=parallel_n) as pool:
        futs = {pool.submit(_generate_udiff, st, plan, repo, ticket_id): st
                for st in batch}
        for fut in as_completed(futs):
            st = futs[fut]; udiffs[st["id"]] = fut.result()
    # Apply serially in original step order.
    for st in batch:
        _apply_and_commit(st, udiffs[st["id"]], ticket_id, repo_path_for_rollback)
```

The current step body (CRITIC retry loop, head-rollback bookkeeping, doer
counters) wraps each generate-then-apply pair. To split, factor that body
into `_generate_udiff(...)` (LLM-only, no git) + `_apply_and_commit(...)`
(git-only, no LLM). Both can land in the same module; the existing
breaker / counter calls move into `_apply_and_commit`.

## Default behaviour

`AIFORGE_DOER_PARALLEL_N=1` keeps today's serial path. Operators opt into
parallelism per-ticket. Once stable, flip default to `4` for LM Studio
backends; cloud (`ollama_cloud`) stays at `1` (rate-limit risk).

## Test plan (next session)

1. Implement `_topo_batches`, `_generate_udiff`, `_apply_and_commit`.
2. Re-run the 10-file Javadoc benchmark with `AIFORGE_DOER_PARALLEL_N=4`.
3. Compare wall time + completion ratio against today's serial baseline:
   - QWEN serial: 586s, 10/10 files
   - QWEN parallel-4 expected: ~180s, 10/10 files
4. Confirm git tree consistency (sequential apply preserves history).

## Out of scope

- Parallel verifier / tester / architect calls — those don't iterate per file today.
- Cloud parallelism (`ollama_cloud`) — gated behind a separate env var because
  rate-limit behaviour differs per tier.
