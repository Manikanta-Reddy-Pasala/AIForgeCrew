---
name: deprecate-and-remove
description: Procedure to safely retire code, an endpoint, or a feature
triggers: [deprecate, remove feature, delete code, sunset, retire, cleanup]
source: builtin
---

1. **Find all callers/usages** (grep, call-graph, analytics). Know the blast radius before touching anything.
2. **Deprecate first**: mark it (annotation/comment/doc), warn on use, announce the timeline + the replacement. Don't yank it out from under callers.
3. **Migrate callers** to the replacement; ship those changes; verify nothing else depends on it.
4. **Remove** only after callers are gone and the deprecation window has passed. Delete code + tests + docs + config together.
5. **Verify**: full test suite + build green; grep confirms zero references remain; no dead config/flags left behind.
6. One focused PR for the removal so it's easy to review and revert.
