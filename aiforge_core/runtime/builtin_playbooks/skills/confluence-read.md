---
name: confluence-read
description: How to read and summarize a Confluence page, including the Jira tickets it references, its comments, and its images.
triggers: [read confluence, confluence page, explain page, understand page, wiki page, whats on the page]
scope: global
---
# Read a Confluence page

1. If given a space name loosely, call `confluence_resolve_space`; find the page
   by exact title with `confluence_page_by_title` to get its id.
2. Call `context_gather` with `kind: "confluence"`, `key: <PAGE-ID>`. This
   fetches the page, the Jira tickets it references, and its images IN PARALLEL,
   cached in the page's folder.
3. From the dossier, summarize concisely:
   - **Purpose** — 2–4 lines; plus the key sections.
   - **Comments** — one line per commenter.
   - **Images / attachments** — one line each (vision caption or extracted
     text; if none, list the filenames).
   - **Linked Jira tickets** — one line each.
4. Summarize — do not paste the raw page body.
