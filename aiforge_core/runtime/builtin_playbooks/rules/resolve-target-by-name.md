---
name: resolve-target-by-name
description: Always resolve a repo/service/folder, Jira project, or Confluence space that the user names loosely, using the resolver tools, before acting on it.
triggers: [repo, service, folder, directory, codebase, project, space]
scope: global
alwaysApply: true
---
# Resolve targets by name — tolerate loose typing

The user will name a code repo, service, folder, Jira project, or Confluence
space however they remember it — different case, spaces, missing hyphens or
underscores, or a small typo. Never assume; resolve it first.

- When a task references a **code repo / service / folder**, call `resolve_repo`
  with the user's wording to get the real local path. Do NOT assume the current
  directory is the intended repo.
- For a **Jira project** named loosely, call `jira_resolve_project`.
- For a **Confluence space** named loosely, call `confluence_resolve_space`.
- These matchers tolerate case, spaces, missing hyphens/underscores, and small
  typos, and return the canonical path/key.
- If a resolver returns `ok: false` with `candidates`, **ask the user which one**
  — do not guess between them.
- Once resolved, operate on the returned path/key for the rest of the task.
