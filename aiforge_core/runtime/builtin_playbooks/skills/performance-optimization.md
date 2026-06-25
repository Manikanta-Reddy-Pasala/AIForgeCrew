---
name: performance-optimization
description: Make code faster by measuring first, not guessing
triggers: [performance, slow, optimize, latency, profile, bottleneck, memory]
source: builtin
---

Measure → find the hot spot → fix the biggest one → re-measure. Never optimize on a hunch.

1. **Reproduce + measure** with a realistic workload. Establish a baseline number (time/throughput/memory).
2. **Profile** to find the actual bottleneck (CPU profiler, query log, flame graph). It's rarely where you'd guess.
3. **Fix the dominant cost first:**
   - Algorithmic: O(n²)→O(n log n); avoid work in loops; cache/memoize pure results.
   - I/O: batch queries (kill N+1), add the right index, paginate, stream instead of loading all.
   - Concurrency: parallelize independent work; don't block the event loop.
4. **Re-measure** against the baseline — confirm the win is real and you didn't break correctness.
5. Stop when it's fast enough. Don't trade readability for micro-gains that don't move the number.
