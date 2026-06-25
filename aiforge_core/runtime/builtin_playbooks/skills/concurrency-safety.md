---
name: concurrency-safety
description: Write correct concurrent/async code without races or deadlocks
triggers: [concurrency, async, thread, race condition, deadlock, lock, parallel, mutex]
source: builtin
---

- **Shared mutable state is the enemy.** Prefer immutability, message-passing, or per-task ownership over shared locks.
- **If you must lock**: keep critical sections tiny; never do slow I/O (or call an LLM/network) while holding a lock; always release on every path (use `with`/RAII).
- **Consistent lock order** everywhere to avoid deadlock; don't acquire lock B while holding A in one place and the reverse in another.
- **Don't block the event loop** in async code — `await` real I/O; offload CPU-bound work to a thread/process pool.
- **Atomicity**: read-modify-write needs a lock or an atomic/transaction; check-then-act is a race.
- **Bound everything**: timeouts, queue sizes, worker pools — so a stall can't hang or OOM the system.
- Test with concurrency (parallel calls), not just sequentially.
