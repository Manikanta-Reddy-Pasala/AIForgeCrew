---
name: safe-refactoring
description: Change structure without changing behavior, keeping tests green throughout
triggers: [refactor, clean up, restructure, rename, extract, simplify, tech debt]
source: builtin
---

Refactoring = behavior-preserving structural change. If behavior changes, it's not a refactor.

1. **Green baseline.** Ensure the relevant tests pass FIRST. No tests for the area? Add a characterization test that pins current behavior before you touch it.
2. **Small steps.** Rename → run tests. Extract a function → run tests. Never batch ten changes then run once — you won't know which broke it.
3. **Tools over hands** where possible (IDE rename, automated extract) — they preserve references the eye misses.
4. **One concern per commit.** A pure refactor commit should have ZERO behavior change and a green suite. Keep refactors out of feature/bugfix diffs so review is clean.
5. **Watch the blast radius.** Before renaming/moving a widely-used symbol, grep its callers; update or verify each.

Stop and reconsider if "just a refactor" starts changing outputs, touching config, or growing past a reviewable diff — that's a feature in disguise; split it out.
