---
name: caching-strategy
description: Add caching that speeds things up without serving stale/wrong data
triggers: [cache, caching, redis, memoize, invalidation, ttl, stale]
source: builtin
---

"There are only two hard things… cache invalidation." Add a cache only after you've measured the cost it removes.

- **Cache the expensive + reused + slowly-changing**: query results, computed aggregates, remote calls. Not cheap or per-request-unique data.
- **Key precisely**: include every input that changes the result (tenant, params, version) — a too-broad key serves wrong data across users.
- **Invalidation plan up front**: TTL (simple, allows staleness) and/or explicit eviction on write. Decide the acceptable staleness window.
- **Stampede protection**: avoid every client recomputing on expiry (lock/single-flight, jittered TTL).
- **Never cache** authz decisions, secrets, or per-request-correct data without care.
- **Measure hit rate** + correctness; a low-hit cache is just overhead + a consistency risk.
