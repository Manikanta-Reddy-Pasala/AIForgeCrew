---
name: authoring-skills-and-workflows
description: Write a good reusable SKILL.md / WORKFLOW.md the agents will actually use
triggers: [skill, workflow, playbook, authoring, write a skill, custom skill, override]
source: builtin
---

Skills/workflows are markdown playbooks auto-surfaced to the agent by relevance. Make them tight + triggerable.

- **One focused job per file.** A skill = a how-to; a workflow = an end-to-end procedure. Don't bundle ten topics.
- **Frontmatter**: `name` (kebab-case, unique), `description` (one line — used for relevance), `triggers` (the words a user/ticket would actually say). Good triggers = it surfaces when needed.
- **Commands, not prose**: "run `pytest && ruff check .`" beats "make sure to test." Agents act on imperatives.
- **Be opinionated + concise** (~10-25 lines). State the steps, the rules, and the common anti-patterns. Long files get truncated in context.
- **Override built-ins** by putting a file with the SAME `name` in `<repo>/.aiforge/skills` (or `.claude`/`.openhands/skills`), and the equivalent `…/workflows`. Repo-local wins over the global default.
- After writing, `skill_search`/`workflow_search` to confirm it surfaces for the expected query.
