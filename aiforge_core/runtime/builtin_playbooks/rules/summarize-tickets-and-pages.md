---
name: summarize-tickets-and-pages
description: When reading a Jira ticket or Confluence page, follow the read skills and always summarize its description, comments, and images, plus its linked tickets/pages.
triggers: [jira, ticket, confluence, page, summarize, explain, understand]
scope: global
alwaysApply: false
---
# Summarize tickets & pages — follow the read skills

When asked to explain or understand a Jira ticket or a Confluence page:

- Use the `jira-read` / `confluence-read` skills — they gather the entity, its
  linked items, and its images in parallel (via `context_gather`).
- A Jira ticket often links Confluence pages, and a Confluence page often
  references Jira tickets — cover **both directions**.
- Always return, concisely (never dump raw bodies):
  - **Description** — a 2–4 line summary.
  - **Comments** — the key point/decision per commenter.
  - **Images & attachments** — one line each, from the vision caption or
    extracted text. If no vision model is configured, list the filenames and
    say they could not be described.
  - **Linked items** — one line per linked ticket/page.
