---
name: testing-practices
description: Write and run tests that actually catch regressions
triggers: [test, unit test, integration test, coverage, pytest, jest, assertion]
source: builtin
---

Tests are an executable spec. Match the project's framework and layout — don't invent a new one.

- **Test behavior, not internals.** Assert on observable outputs/effects; avoid coupling to private structure so refactors don't break tests.
- **Arrange–Act–Assert**, one logical assertion per test, descriptive name (`<unit>_<condition>_<expected>`).
- **Cover the edges**: empty/null, boundary values, error paths, concurrency, large input — not just the happy path.
- **Deterministic**: no real network/clock/random/sleep — inject or mock them. A flaky test is worse than no test.
- **Fast feedback**: run the focused test while iterating; run the FULL relevant suite before claiming done.
- New bug → add a failing test that reproduces it FIRST, then fix.
