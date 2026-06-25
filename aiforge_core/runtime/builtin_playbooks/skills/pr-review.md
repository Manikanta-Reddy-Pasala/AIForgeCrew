---
name: pr-review
description: Review someone else's change for correctness, risk, and clarity
triggers: [review, pr review, code review, pull request, merge request, approve]
source: builtin
---

Review the diff, not the author. Be specific and kind; block on real problems, suggest on the rest.

Check, in order:
1. **Correctness** — does it do what the PR claims? Trace the main path + the obvious edge cases. Off-by-one, null/empty, error handling.
2. **Tests** — is the new behavior covered? Does a test fail without the change?
3. **Security** — input validation, authz, injection, secrets, unsafe deserialization.
4. **Blast radius** — backward compat, migrations, callers of changed signatures.
5. **Clarity** — names, dead code, leftover TODOs/prints, diff scope (unrelated changes?).
6. **Simplicity** — is there a smaller/clearer way? Don't gold-plate.

Verdict: approve / request-changes with concrete, actionable comments (quote the line, say why, propose the fix).
