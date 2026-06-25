---
name: issue-triage
description: Turn a vague bug report into a reproducible, prioritized, routed issue
triggers: [triage, bug report, issue, prioritize, reproduce, severity, backlog]
source: builtin
---

- **Reproduce first**: exact steps, environment, version, expected vs actual. If you can't reproduce, ask for the missing detail (logs, repro, screenshot) — don't guess.
- **Classify**: bug vs feature vs question vs duplicate. Search for an existing/duplicate issue before opening a new one.
- **Severity × frequency = priority**: data loss/security/outage = top; cosmetic-rare = low. Be honest about impact and how many users hit it.
- **Scope it**: what's the smallest fix? Is the root cause known or does it need investigation?
- **Route**: label (area/component), assign or queue to the right owner, link related issues.
- **Make it actionable**: a good issue has a clear title, repro, expected behavior, and acceptance criteria so whoever picks it up can start immediately.
