---
name: jira-ticket-to-mr
description: End-to-end from a Jira ticket to a merge request — read the ticket, identify the service/repo, fix the bug, branch and commit tied to the ticket, then open the MR.
triggers: [fix this ticket, jira to mr, implement ticket, ticket to merge request, fix and open mr, work on jira]
scope: global
---
# Jira ticket → merge request

Follow these steps IN ORDER; produce nothing until a step's prerequisite is met.

## STEP 1 — Read the ticket
Call `context_gather` (kind: jira, key: <TICKET-KEY>) and follow the `jira-read`
skill. Restate the requirement in one line. Include anything from the ticket's
linked Confluence pages and images that affects the fix.

## STEP 2 — Identify the service / repo
From the ticket's project, component, or text, determine which code repo the fix
belongs in, then call `resolve_repo` with that name to get its local path
(tolerates loose names). If the resolver is ambiguous, ASK which repo. Work in
the resolved path — never assume the current directory.

## STEP 3 — Confirm it is a code fix
If the ticket is analysis / documentation / a question only (no code change),
stop and summarize instead of branching.

## STEP 4 — Branch
Create `feature/<TICKET-KEY>-<short-kebab-description>` in the resolved repo.

## STEP 5 — Fix
Make the minimal change that resolves the ticket. Add or update a test that
covers it. Run the tests until green.

## STEP 6 — Commit
Commit message MUST start with `[<TICKET-KEY>] fix: <what changed>`.

## STEP 7 — Open the merge request
Push the branch and open the MR/PR (`gitlab_mr_create` or `github_pr`): title
`[<TICKET-KEY>] <summary>`, description links the Jira ticket.

## STEP 8 — Link back
Add a Jira comment (`jira_comment`) with the MR link.

Honour the `git-branch-commit-convention` and `resolve-target-by-name` rules
throughout.
