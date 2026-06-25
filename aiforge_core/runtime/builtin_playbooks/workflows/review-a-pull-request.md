---
name: review-a-pull-request
description: End-to-end procedure to review a PR and leave an actionable verdict
triggers: [review pr, review pull request, code review, approve pr, merge request review]
source: builtin
---

1. **Understand intent**: read the PR title/description + linked ticket. What problem, what approach, what's the claimed scope?
2. **Pull + build**: check out the branch, install, run the test suite + lint/typecheck locally. Red CI = stop here, report it.
3. **Read the diff with intent** (see the `pr-review` skill): correctness → tests → security → blast radius → clarity → simplicity.
4. **Verify the claim**: does a test actually cover the new behavior? Try one edge case the PR didn't mention.
5. **Leave specific comments**: quote the line, state the problem, propose the fix. Separate must-fix (blocking) from nits (non-blocking).
6. **Verdict**: approve / request-changes / comment. Summarize the top 1–3 things in a closing note.
