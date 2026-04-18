---
name: aiforge-lifecycle
description: Transition an AIForgeCrew ticket through the DESIGN §4 TDD lifecycle. Use this when you must advance a ticket state (planning → tests_writing → coding → verifying → reviewing → mr_created → merged) or loop back after a verify/review failure. Every transition triggers §10 gate checks (loop caps, coverage ≥80 for mr_created).
version: 1.0.0
platforms: [macos]
prerequisites:
  commands: [{{AIFORGE_BIN}}]
---

# aiforge-lifecycle

Advance an AIForgeCrew ticket through its TDD lifecycle. Transitions are rejected if:
- Invalid (e.g. `created → reviewing` is not allowed).
- dev↔tester or dev↔architect loop cap exceeded (§10).
- `mr_created` with no recent `coverage` audit event at ≥80%.

## When to use

- After EM finishes planning → `planning` then `tests_writing`.
- After Tester commits failing tests → `coding` (routes to Sr Dev).
- After Sr Dev pushes code → `verifying` (routes to Tester).
- After Tester re-runs + records coverage → `reviewing` (pass) or `coding` (fail).
- After Sr Architect LGTMs → `mr_created` (routes to human).

## Allowed next states

```bash
{{AIFORGE_BIN}} ticket show TICKET-xxx
# → "Allowed next states: ..." line at the bottom
```

## Advance

```bash
{{AIFORGE_BIN}} ticket advance TICKET-xxx --to planning --actor em
{{AIFORGE_BIN}} ticket advance TICKET-xxx --to tests_writing --actor em
{{AIFORGE_BIN}} ticket advance TICKET-xxx --to coding --actor tester
{{AIFORGE_BIN}} ticket advance TICKET-xxx --to verifying --actor sr-developer
{{AIFORGE_BIN}} ticket advance TICKET-xxx --to reviewing --actor tester
{{AIFORGE_BIN}} ticket advance TICKET-xxx --to mr_created --actor sr-architect
{{AIFORGE_BIN}} ticket advance TICKET-xxx --to merged --actor human
```

## Loop-back (verify failed)

```bash
{{AIFORGE_BIN}} ticket advance TICKET-xxx --to coding --actor tester
```

After 3 loops, the next attempt raises `RetryExceeded` and auto-records
`event=escalate` in the audit — the ticket must then be escalated to human.

## Exit codes

- 0 — transition accepted
- 1 — invalid transition / coverage gate failed / loop cap exceeded
- 4 — permission denied (role cannot advance)

## Notes

- `actor` must match a known role: `em | tester | sr-developer | sr-architect | human`.
- Assignee is auto-rotated per `paperclip.config.yml:routing`.
- Coverage is recorded via the `aiforge-coverage` skill (run that BEFORE attempting `mr_created`).
