---
name: jira-read
description: How to read and summarize a Jira ticket, including its linked Confluence pages, comments, and images.
triggers: [read jira, jira ticket, explain ticket, understand ticket, look at jira, whats in the ticket]
scope: global
---
# Read a Jira ticket

1. If the project/key was given loosely, call `jira_resolve_project` first.
2. Call `context_gather` with `kind: "jira"`, `key: <TICKET-KEY>`. This fetches
   the issue, its linked Confluence pages, and its images IN PARALLEL and caches
   them in the ticket's folder (so a re-read is instant and refreshes only when
   the ticket changed).
3. From the returned dossier, produce a concise summary:
   - **Description** — 2–4 lines; plus status, assignee, priority.
   - **Time** — original estimate vs. time spent (from the ticket's tracking);
     use `jira_worklog` if the user asks who logged what.
   - **Comments** — the key point per author.
   - **Images / attachments** — one line each (vision caption or extracted
     text; if none, list the filenames).
   - **Linked Confluence pages** — one line each.
4. Summarize — never paste the raw description or comment bodies.
