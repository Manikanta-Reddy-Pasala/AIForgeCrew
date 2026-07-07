# Skills, Rules & Workflows — how they work

Three kinds of reusable instruction the agents pull in automatically. All are
plain markdown, managed in the **Library** UI (`/library`), authored in chat
(`New … via chat`), or written by the agent itself when it solves something
reusable. **They ship EMPTY** — nothing is bundled; you add what your team needs.

| Kind | What it is | When the agent uses it |
|------|------------|------------------------|
| **Skill** | Reusable know-how for a *kind of task* (e.g. "add a Stripe webhook"). Has `triggers`. | Pulled in **on relevance** — when the request matches its triggers/description. |
| **Workflow** | An end-to-end *procedure* (ship a feature, fix a bug), ordered steps. Has `triggers`. | Same relevance match; surfaces the whole recipe. |
| **Rule** | An always-on *constraint* (e.g. "never commit secrets", "match conventions"). | Injected **every turn** (if `alwaysApply`) or when a changed file matches its `globs`. |

## How selection works

- **Skills / workflows** are ranked by relevance to the current request:
  an exact trigger hit scores high, plus token overlap with the name /
  description / triggers. The top matches are injected into the agent's context
  — so a well-chosen `triggers:` list is what makes a skill fire.
- **Rules** are not ranked — an `alwaysApply: true` rule is in context on every
  turn; a glob-scoped rule applies when the files under edit match its `globs`.

## Where they live & precedence

Loaded and merged from (later wins on a name clash):

1. **Global** — `~/.aiforge/{skills,workflows,rules}` (this machine, all repos).
2. **Repo-local** — `<repo>/.aiforge/…`, `<repo>/.claude/…`, `<repo>/.openhands/…`.

A **custom** item always overrides a same-named default. There are no bundled
defaults anymore — the library starts blank.

## Creating them

- **Library UI** (`/library/skills|workflows|rules`) — write by hand, or
  "Generate draft" with the model, then Save. **Delete** (per item) and
  **Clear all** buttons remove them.
- **In chat** — `New … via chat` runs a short builder interview, then saves.
- **The agent, automatically** — after solving something hard it calls
  `write_skill` / `write_workflow` / `remember_rule`, so next time it's recalled.

## Authoring format

```markdown
---
name: add-stripe-webhook
description: Add and verify a Stripe webhook endpoint      # used for matching
triggers: [stripe, webhook, payment]                       # skills/workflows
---

1. Concrete, ordered steps the agent follows…
```

Rules use `alwaysApply: true` (or `globs: src/**/*.ts`) instead of triggers, and
a `# Title` + tight imperative bullets:

```markdown
---
name: no-secrets
alwaysApply: true
---
# No secrets in code
- Read secrets from env / a secret store; never hard-code or log them.
```

## Tips

- **Triggers are everything** for skills/workflows — list the words a user would
  actually say. No trigger match = it won't fire.
- **Generalize** — strip one-off paths/ids so it applies next time. A one-off
  isn't a skill.
- **Rules are for constraints**, not knowledge — keep them short and testable;
  they cost context on every turn.
- **Scope** — put team-wide items in the global dir; repo-specific ones in
  `<repo>/.aiforge/…` so they only load for that repo.
