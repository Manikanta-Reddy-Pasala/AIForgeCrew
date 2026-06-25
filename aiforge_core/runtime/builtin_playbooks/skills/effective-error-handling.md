---
name: effective-error-handling
description: Handle errors so failures are clear, safe, and recoverable
triggers: [error handling, exception, try catch, fail, resilience, retry, fallback]
source: builtin
---

- **Fail fast + loud at boundaries**, fail safe in the middle. Validate input early; don't let a bad value travel.
- **Catch narrowly.** Catch the specific error you can handle; don't swallow everything. A bare catch that hides the error is a future 3am bug.
- **Add context, then re-raise/wrap**: include what you were doing + the inputs, but don't leak secrets/internals to users.
- **Don't return null/empty to signal failure** silently — raise, or return a Result/Either the caller must handle.
- **Transient vs permanent**: retry transient (timeout, 5xx, lock) with bounded backoff; don't retry a 400/validation error.
- **Clean up on every path** (close files/conns/locks) — `finally`/`with`/defer.
- **User-facing message** is actionable + safe; the log has the detail.
