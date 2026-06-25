---
name: onboard-to-a-new-repo
description: Procedure to get productive in an unfamiliar repository
triggers: [onboard, new repo, new project, get started, clone, first task]
source: builtin
---

1. **Read the map**: README, CONTRIBUTING, AGENTS.md/CLAUDE.md, architecture docs. Note conventions + the golden-path commands (install/build/test/run).
2. **Get it running**: install deps, build, run the test suite, start the app. A green baseline proves your env works before you change anything.
3. **Trace one real flow** end to end (see `reading-unfamiliar-code` skill) to learn the layering.
4. **Find the conventions**: naming, error handling, test layout, lint/format config — you'll mirror these.
5. **Make a tiny safe change** (fix a typo, add a test) and run the full verify loop to learn the contribution workflow.
6. Only then take a real task — now you know where things live and how to prove your change.
