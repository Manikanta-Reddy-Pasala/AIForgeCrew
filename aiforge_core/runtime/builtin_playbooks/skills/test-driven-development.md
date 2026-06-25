---
name: test-driven-development
description: Implement a feature or fix by writing the test first
triggers: [tdd, test, write a test, new feature, implement, bugfix]
source: builtin
---

Red → Green → Refactor. The test defines "done" before you write code.

1. **Red** — write the smallest test that captures the desired behavior (or reproduces the bug). Run it; watch it FAIL for the right reason. A test that passes immediately tested nothing.
2. **Green** — write the *minimum* code to make it pass. No gold-plating, no unrelated changes.
3. **Refactor** — with the test green, clean up names/duplication/structure. Re-run; stay green.
4. Repeat one behavior at a time. Keep each cycle small enough to hold in your head.

Rules:
- Test behavior, not implementation details — assert on outputs/effects, not private internals.
- One logical assertion per test; a clear name (`test_<unit>_<condition>_<expected>`).
- Match the project's existing test framework/layout — don't invent a new one.
- Before claiming done: run the FULL relevant suite, not just the new test.
