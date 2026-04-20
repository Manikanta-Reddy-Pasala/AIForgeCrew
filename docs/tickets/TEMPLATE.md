# <TICKET-ID> — <Title>

**Written by**: Software Architect (Claude Code)
**Date**: <YYYY-MM-DD>
**Ticket ID**: <TICKET-ID>
**Branch**: `aiforge/<TICKET-ID>` (created by Architect in all involved repos below)

## Involved repos

- `<repo-1>` — <why touched>
- `<repo-2>` — <why touched>  *(only list if actually needed)*

## Problem

<1-3 paragraphs. What is broken or missing. Cite specific file:line if known. Symptoms user sees.>

## Why this matters

<1 paragraph. Business impact, severity, who's blocked.>

## Design choice

<The Architect chose an approach. State it here. Rationale in 1 paragraph. Alternatives considered + why rejected.>

## Acceptance criteria

- [ ] <testable statement 1>
- [ ] <testable statement 2>
- [ ] <testable statement 3>

## Files likely touched

- `<repo>/<path/to/file.java>` — <change summary>
- `<repo>/<path/to/file.py>` — <change summary>

## Reference patterns (prior art)

- `<file:line>` — <why relevant>
- Commit `<sha>` — <prior fix touching same area>
- rag query suggestion: `rag "<topic>"`

## Constraints / non-goals

- DO NOT <thing>
- OUT OF SCOPE: <thing>

## Test strategy (hint for Sr Dev)

<1 paragraph on how to verify the fix. What edge cases. What invariants.>

---

**Sr Developer**: read this file, then write `docs/breakdowns/<TICKET-ID>.md` with numbered sub-tasks + test case per sub-task. Post breakdown comment on ticket with `READY_FOR_DEV`.

**Developer**: after Sr Dev's breakdown exists, implement sub-tasks one at a time, commit per sub-task, push, open PR. Post final comment with `READY_FOR_REVIEW`.
