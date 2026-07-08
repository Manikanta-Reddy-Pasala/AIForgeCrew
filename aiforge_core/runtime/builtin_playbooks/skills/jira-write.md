---
name: jira-write
description: How to create or update a Jira ticket, add comments, and log work — safely, through the approval gate.
triggers: [create jira, new ticket, raise a ticket, update ticket, comment on jira, log work, write jira]
scope: global
---
# Write to Jira

1. Resolve the project with `jira_resolve_project` if it was named loosely.
2. Choose the action:
   - **Create** — `jira_create` with `project`, `summary` (one line), `issuetype`
     (default Task), `description` (the detail), optional `labels`.
   - **Update fields** — `jira_update` with `key` + the fields to change.
   - **Comment** — `jira_comment` with `key`, `body`.
   - **Log time** — `jira_log_work` with `key`, `time_spent` (e.g. `"2h 30m"`).
3. Every write is approval-gated: state exactly what you will write (project,
   summary, key fields), then let the user Approve/Reject before it goes out.
4. Keep summaries short and put the substance in the description/comment body.
