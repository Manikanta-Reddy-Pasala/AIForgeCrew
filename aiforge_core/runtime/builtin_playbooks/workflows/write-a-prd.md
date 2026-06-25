---
name: write-a-prd
description: Procedure to write a product requirements doc / spec before building
triggers: [prd, spec, requirements, product doc, design doc, proposal, plan a feature]
source: builtin
---

Write the spec before the code for anything non-trivial — it surfaces disagreement cheaply.

1. **Problem + why now**: the user problem, who has it, evidence it matters. Not the solution yet.
2. **Goals + non-goals**: what success looks like (measurable), and explicitly what's OUT of scope.
3. **Users + scenarios**: the key flows/use-cases in plain language.
4. **Requirements**: functional (what it must do) and non-functional (performance, security, scale, a11y). Prioritize (must/should/could).
5. **Approach + alternatives**: the proposed solution at a high level; options considered + why this one (link an ADR for big tech choices).
6. **Risks, dependencies, rollout**: what could go wrong, what it depends on, how it ships (flag/phased).
7. **Success metrics**: how you'll know it worked.
Keep it as short as the problem allows; circulate for review BEFORE implementation.
