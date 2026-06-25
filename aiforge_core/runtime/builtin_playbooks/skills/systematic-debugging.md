---
name: systematic-debugging
description: Find the root cause of a bug methodically instead of guessing
triggers: [bug, debug, error, exception, failing test, crash, regression]
source: builtin
---

When something is broken, do NOT guess-and-patch. Work the cause, not the symptom.

1. **Reproduce reliably.** Get a minimal, deterministic repro (one command / one test). If it's flaky, make it consistent before anything else.
2. **Read the actual error.** Full message + stack trace, top frame in *your* code. Don't skim — the real cause is often one line above where it surfaced.
3. **Locate, don't speculate.** grep/ripgrep for the failing symbol; read the enclosing function. Add a focused log/print at the boundary to confirm the bad value's origin.
4. **Form ONE hypothesis** and test it cheaply (a print, a unit test, a debugger breakpoint). Confirm or kill it before forming the next.
5. **Fix the root cause**, then add/repair a test that fails before the fix and passes after — so the bug can't silently return.
6. **Verify**: run the repro + the surrounding test suite. State the evidence (output), don't assume.

Anti-patterns: changing multiple things at once, suppressing the error, widening a catch, blaming the framework before checking your own code.
