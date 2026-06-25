---
name: frontend-design
description: Make UI look intentional and distinctive, not templated-default
triggers: [ui, ux, design, frontend design, styling, layout, visual, css, theme]
source: builtin
---

Technical correctness isn't enough — UI should feel deliberate.

- **Typography first**: a clear type scale (one or two families), consistent weights/sizes; generous line-height; limit to ~2-3 sizes per view.
- **Spacing system**: use a consistent scale (4/8px steps), not arbitrary pixels. Whitespace is a feature; let things breathe.
- **Restrained color**: a small palette + one accent. Sufficient contrast (WCAG AA). Don't decorate — colour communicates state/hierarchy.
- **Hierarchy**: the eye should land on the primary action first. Size/weight/colour establish importance.
- **Avoid the default look**: don't ship raw framework defaults (unstyled borders, default fonts/shadows). Small intentional choices read as quality.
- **Consistency** > novelty: reuse components, align edges, match interaction patterns across the app.
- **States**: design hover/focus/active/disabled/loading/empty/error — not just the happy state.
