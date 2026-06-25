---
name: qa-a-change
description: Procedure to manually verify a change really works before merging
triggers: [qa, verify, manual test, smoke test, acceptance, check it works, validate]
source: builtin
---

Tests passing ≠ feature works. Exercise the real thing.

1. **Re-read the requirement / acceptance criteria** — what is "works" exactly?
2. **Run the app/feature for real** (not just unit tests): the happy path end to end, as a user would.
3. **Try the edges**: empty/invalid input, the error path, boundary values, permissions, a second concurrent action.
4. **Check it didn't break neighbors** (regression): the adjacent features that touch the same code/data.
5. **Verify side effects**: DB rows, emitted events, logs, files — the effects, not just the response.
6. **Confirm the automated gate too**: full suite + lint/typecheck green.
7. Record what you verified (commands + observed result) in the PR so a reviewer can trust it.
