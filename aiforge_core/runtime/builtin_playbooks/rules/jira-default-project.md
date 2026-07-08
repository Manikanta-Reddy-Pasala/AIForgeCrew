---
name: jira-default-project
description: The organization's default Jira project is CLR — use it for reads, searches, and new tickets unless the user names a different project.
triggers: [jira, ticket, project, issue]
scope: global
alwaysApply: true
---
# Default Jira project — CLR

Unless the user explicitly names a different Jira project:

- Use project **CLR** for Jira searches, reads, and newly-created tickets.
- When resolving a Jira project from a loose name and the intent is unclear,
  prefer **CLR**.
- A bare ticket key the user gives (e.g. `123`) refers to `CLR-123`.

If the user names another project, honour that project for the task instead.
