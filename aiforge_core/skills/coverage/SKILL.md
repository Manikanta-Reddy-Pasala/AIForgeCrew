---
name: aiforge-coverage
description: Record a `coverage` audit event on a ticket so the architect can transition it to `mr_created`. Coverage must be ≥80% per DESIGN §10. Use immediately after Tester finishes the verify pass.
version: 1.0.0
platforms: [macos]
---

# aiforge-coverage

Emit a `coverage` audit row. The `aiforge-lifecycle` gate blocks
`reviewing → mr_created` unless the most recent coverage event is ≥80%.

## Record coverage

```bash
{{AIFORGE_PY}} - <<'PY'
from pathlib import Path
from aiforge_core.store import Store
s = Store(Path(".paperclip/paperclip.db"))
s.audit_event("TICKET-xxx", "coverage", "tester", {"pct": 91.0, "pass": 14, "total": 14})
PY
```

## Required data fields

- `pct` (float) — coverage percentage
- `pass` (int) — tests that passed
- `total` (int) — total tests

## When to use

- Immediately after `pytest --cov` completes successfully during the Tester verify stage.
- Always before requesting `reviewing → mr_created`.

## Below threshold

If `pct < 80`, record it anyway (for auditability) + advance the ticket back
to `coding` via `aiforge-lifecycle` (counts as a dev↔tester loop).
