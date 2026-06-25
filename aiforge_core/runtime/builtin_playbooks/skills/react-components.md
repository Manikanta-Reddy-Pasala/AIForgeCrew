---
name: react-components
description: Build correct, performant React components with hooks
triggers: [react, hooks, useeffect, usestate, component, jsx, render, frontend]
source: builtin
---

- **Function components + hooks.** Keep components small and focused; lift state only as high as needed.
- **`useEffect` is for synchronizing with the outside world** (subscriptions, fetch), not derived state. Derive during render; don't `setState` in an effect to compute from props.
- **Dependency arrays must be honest** — include every value used. Missing deps = stale closures; lying to the linter causes bugs.
- **Stable identities**: memoize callbacks/objects passed as props (`useCallback`/`useMemo`) to avoid needless re-renders; a new inline object each render breaks memo children.
- **Keys** on lists must be stable + unique (not the array index when items reorder).
- **Cleanup** effects (unsubscribe, abort fetch) to avoid leaks/race on unmount.
- **State updates are async + batched**; use the functional updater when the next value depends on the previous.
- Keep side effects out of render; render must be pure.
