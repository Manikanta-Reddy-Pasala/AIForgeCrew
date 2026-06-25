---
name: upgrade-a-dependency
description: Procedure to upgrade a dependency without breaking the build
triggers: [upgrade dependency, bump version, update package, dependency upgrade]
source: builtin
---

1. **Branch** `chore/bump-<dep>`.
2. **Read the changelog** between current and target versions — note breaking changes + required code edits.
3. **Bump one** (or one cohesive group); regenerate the lockfile.
4. **Clean install** from scratch; **run tests + lint/typecheck**. Fix breakages guided by the changelog, not by guessing.
5. **Run a security audit**; confirm the upgrade closes (not opens) vulns.
6. **Smoke-test** the affected feature manually if it touches runtime behavior.
7. Commit with the version delta + why; PR notes any required follow-up.
