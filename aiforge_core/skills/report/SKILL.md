---
name: aiforge-report
description: Per-ticket and fleet-wide reports from the AIForgeCrew audit table — tokens per role, tool calls, transitions, loop counters, coverage events, stalled tickets, budget status.
version: 1.0.0
platforms: [macos]
---

# aiforge-report

## Per-ticket report

```bash
{{AIFORGE_BIN}} report-ticket TICKET-xxx | jq .
```

Returns `{tokens_per_role, tool_calls_per_role, transitions, loops, comment_count, escalated, duration_s}`.

## Fleet summary

```bash
{{AIFORGE_BIN}} report-fleet | jq .
```

Returns `{total_tickets, by_state, tokens_per_role, stalled_tickets, budgets}`.

## Budget report

```bash
{{AIFORGE_BIN}} budget-report               # all roles
{{AIFORGE_BIN}} budget-report --role em     # one role
```

Shows month-USD spend vs cap + tokens-per-ticket cap.

## Audit drill-down

```bash
{{AIFORGE_BIN}} audit TICKET-xxx
```

Prints every append-only event: creates / comments / transitions / assigns / budget / tool_call / coverage / breaker_* / escalate.

## When to use

- EM: weekly fleet summary, spot stalled tickets.
- Sr Architect: per-ticket report before approving to confirm loop counts + tokens spent.
- Human CEO: monthly budget report.
