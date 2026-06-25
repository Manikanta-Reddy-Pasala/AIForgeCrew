---
name: ship-a-feature
description: End-to-end procedure to implement and ship a code change as a reviewable PR
triggers: [ship, feature, implement, build, pull request, pr, deliver]
source: builtin
---

The full path from a requirement to a mergeable PR. Adapt commands to the project's stack.

1. **Understand + scope.** Restate the requirement. Read the relevant code (grep/repo-map). Identify the few files to touch. If ambiguous, ask one sharp question instead of guessing.
2. **Branch.** Never work on the default branch. `git switch -c <type>/<short-desc>` (feat/fix/chore).
3. **Plan the change** as a short ordered list of edits + the tests that prove each.
4. **Test-first where it fits** (see the `test-driven-development` skill). Otherwise write the test alongside.
5. **Implement** in small steps that read like the surrounding code (match naming, idioms, comment density).
6. **Verify locally** — run the test suite, then the project's lint / format / typecheck (e.g. `ruff`/`eslint`, `mypy`/`tsc`). Fix every failure; don't claim done on red.
7. **Self-review the diff.** Re-read it cold: leftover prints/TODOs, unintended file changes, secrets, scope creep. Keep the diff minimal.
8. **Commit** with a clear message (what + why, not how). One logical change per commit.
9. **Open a PR** with a concise description: what changed, why, how it was verified, any risk. Link the ticket.
10. **Respond to CI + review** — fix failures and address comments rather than arguing; re-run verification after each change.

Definition of done: tests + lint + typecheck green, diff minimal and self-reviewed, PR describes the change and how it was verified.
