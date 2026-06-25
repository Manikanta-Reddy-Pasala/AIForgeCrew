---
name: reading-unfamiliar-code
description: Get oriented in a codebase you've never seen, fast
triggers: [understand code, new codebase, explore, onboard, unfamiliar, read code, navigate]
source: builtin
---

Don't read top-to-bottom. Build a map from the outside in.

1. **Entry points first**: README, run/build scripts, `main`/server bootstrap, routes/CLI commands — how does it start, what does it expose?
2. **Trace one real request/flow** end to end (route → handler → service → data). That teaches the layering faster than any doc.
3. **Find the shape**: directory layout, the few "god" modules, the data models, the config.
4. **Use tools, not eyeballs**: ripgrep for a symbol, the repo-map, call/caller chains, tests (tests document intended behavior).
5. **Run it + run the tests** — observe real behavior before changing anything.
6. Note conventions (naming, error handling, test layout) and MATCH them when you edit.
