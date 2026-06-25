---
name: establish-project-conventions
description: Procedure to write an AGENTS.md/CLAUDE.md so agents follow this repo's rules
triggers: [agents.md, claude.md, conventions, project rules, onboarding doc, contributing]
source: builtin
---

Give agents (and humans) the repo's rulebook so they stop guessing.

1. **Capture the golden-path commands**: exact install, build, test, lint, format, typecheck, run — as COMMANDS, not prose ("run `pnpm test`").
2. **Layout + architecture**: where things live, the layering, the key modules, the data models.
3. **Conventions**: naming, error handling, test layout/framework, commit/PR format, branch rules.
4. **Dos and don'ts**: project-specific gotchas, things that look wrong but are intentional, what NOT to touch.
5. **Keep it focused** (~200-400 lines); in a monorepo, nested AGENTS.md per package keeps context local.
6. **Live with the code**: update it in the same PR when conventions change; if the same mistake recurs across sessions, it belongs here.
7. Place at repo root (`AGENTS.md`/`CLAUDE.md`); the agent reads the nearest one.
