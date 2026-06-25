---
name: architecture-decision
description: Evaluate options and record a sound architectural decision (ADR) for the architect
triggers: [architecture, design, adr, decision, tradeoff, choose, evaluate, technology selection]
source: builtin
---

For any non-trivial design choice (data store, framework, boundary, pattern). Produce a decision AND a durable record.

1. **Frame the problem.** State the decision to make and the forces in play: functional needs, scale/latency targets, team skills, operational cost, deadlines, reversibility.
2. **List 2–4 real options.** For each: a one-line description + how it meets the forces. Include the "do nothing / status quo" option.
3. **Compare on the forces that matter** — not a generic pros/cons dump. Call out the decisive trade-offs (e.g. consistency vs availability, build vs buy, simplicity vs flexibility). Prefer the simplest option that meets real (not imagined) requirements.
4. **Decide, and say why.** Name the chosen option and the single most important reason it won. Note what you're explicitly trading away.
5. **Capture consequences** — what becomes easier, what becomes harder, what to revisit, and the rough cost/effort.
6. **Write an ADR** (a short markdown record): Context · Options considered · Decision · Consequences · Status (proposed/accepted) · Date. Commit it to the repo (`docs/adr/NNNN-title.md`) so future readers know WHY, not just what.

Principles: optimize for changeability and the team's understanding, not cleverness. A reversible decision deserves a fast call; an irreversible one deserves a prototype first. Avoid resume-driven and hype-driven choices.
