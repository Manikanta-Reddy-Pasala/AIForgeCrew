---
name: aiforge-memory
description: Two-tier MemPalace-backed memory. `project` scope = shared across agents (writers EM + sr-architect only, DESIGN §6). `own` scope = per-role private memory. Search defaults to `auto` (own + project).
version: 1.0.0
platforms: [macos]
---

# aiforge-memory

## Remember (own scope, any role)

```bash
{{AIFORGE_PY}} - <<'PY'
from pathlib import Path
from aiforge_core.mem import MemBus
MemBus(Path(".aiforge/mem")).remember(
    role="sr-developer", scope="own",
    title="Fixed JWT expiry bug",
    text="Root cause was `<` vs `<=` in middleware. See TICKET-abc.",
)
PY
```

## Remember (project scope — EM + sr-architect only)

```bash
{{AIFORGE_PY}} - <<'PY'
from pathlib import Path
from aiforge_core.mem import MemBus
MemBus(Path(".aiforge/mem")).remember(
    role="sr-architect", scope="project",
    title="Coverage gate threshold",
    text="We use 80% minimum per DESIGN §10. Raise only after 4 consecutive green releases.",
)
PY
```

## Search (defaults to project + own)

```bash
{{AIFORGE_PY}} - <<'PY'
from pathlib import Path
from aiforge_core.mem import MemBus
hits = MemBus(Path(".aiforge/mem")).search("sr-developer", "coverage gate", scope="auto", limit=5)
for h in hits: print(h)
PY
```

## When to remember

- Sr Dev: non-obvious fixes, hard-to-reproduce bugs, performance traps.
- Tester: flaky areas, fixture gotchas, coverage gaps by module.
- Sr Architect: architecture decisions, security patterns, API contracts.
- EM: sprint context, velocity observations, planning heuristics.
