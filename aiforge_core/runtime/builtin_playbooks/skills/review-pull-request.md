---
name: review-pull-request
description: How to review a pull/merge request — fetch the diff, check it across dimensions, run the tests, and post findings ranked by severity.
triggers: [review pr, review the pr, review pull request, review mr, review merge request, code review, pr review]
scope: global
---
# Review a pull/merge request

1. **Fetch the change** — get the PR/MR diff (`gitlab_read` / the `gh` CLI for a
   GitHub PR). If a repo folder is named loosely, `resolve_repo` first. Read the
   PR title/description and any linked Jira ticket for the intended behaviour.
2. **Review across dimensions** — go through the diff and check:
   - **Correctness** — logic errors, off-by-one, wrong conditionals, mis-wired
     args, does it actually do what the PR claims.
   - **Security / IO** — injection (SQL/JQL/CQL/shell), path traversal, unsafe
     file writes, secrets in code/logs, unvalidated external input.
   - **Concurrency / resources** — races, shared mutable state, leaks, unbounded
     work.
   - **Tests** — is the change covered? Do edge/error paths have tests?
   - **Simplicity** — dead code, duplication, needless complexity.
3. **Verify, don't guess** — for each suspected issue, trace a concrete
   inputs→failure path in the actual code before reporting it; drop anything a
   guard/caller/semantics prevents.
4. **Run the tests** — `run_tests` (and `typecheck`/`lint` if present); note
   failures with the output.
5. **Report** — findings ranked most-severe first, each with file:line, a
   one-line problem statement, and a concrete failure scenario. End with a clear
   verdict: **approve** or **request changes** (with the must-fix list). Post as
   inline PR comments only if the user asks.
Be specific and evidence-based; no style nits unless they hide a real bug.
