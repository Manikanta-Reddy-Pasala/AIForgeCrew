---
name: profile-and-optimize
description: Procedure to diagnose and fix a performance problem
triggers: [optimize performance, fix slow, profile, reduce latency, speed up]
source: builtin
---

1. **Reproduce** the slowness with a realistic, repeatable workload; record a baseline number.
2. **Profile** to find the real bottleneck (CPU/alloc profiler, slow-query log, trace) — don't guess.
3. **Target the dominant cost** (see `performance-optimization` skill): algorithm, N+1/index, blocking I/O, missing cache.
4. **Change one thing**; re-measure against the baseline; confirm correctness still holds (tests green).
5. **Iterate** on the next-biggest cost until it's fast enough — then STOP.
6. Document the before/after numbers and the change so the win is auditable and doesn't regress.
