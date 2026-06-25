---
name: fix-a-bug
description: End-to-end procedure to diagnose, fix, and regression-proof a bug
triggers: [fix bug, bugfix, hotfix, defect, broken, incident]
source: builtin
---

From a bug report to a verified, regression-proofed fix.

1. **Reproduce** with a minimal deterministic case (see the `systematic-debugging` skill). Capture the exact failing command/input and the actual vs expected output.
2. **Branch** off the default: `git switch -c fix/<short-desc>`.
3. **Write a failing test** that encodes the bug (Red). It must fail for the real reason — that's your proof you understood it.
4. **Find the root cause**, not the symptom. Read the failing code path; confirm the bad value's origin before editing.
5. **Fix minimally.** Smallest change that makes the test pass without breaking neighbors.
6. **Verify**: the new test passes, AND the surrounding suite + lint/typecheck stay green.
7. **Check the blast radius** — is the same bug pattern elsewhere? grep for it; fix or note.
8. **Commit + PR** describing the symptom, the root cause, and the fix. Include the repro so reviewers can confirm.

Definition of done: a test that failed before and passes after, full suite green, root cause (not symptom) addressed.
