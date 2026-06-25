---
name: web-accessibility
description: Build UIs that work for everyone (WCAG/a11y)
triggers: [accessibility, a11y, wcag, aria, screen reader, keyboard, contrast]
source: builtin
---

- **Semantic HTML first**: real `<button>`/`<a>`/`<nav>`/`<label>` — they bring keyboard + screen-reader behavior free. ARIA is a patch, not a substitute.
- **Keyboard operable**: every interactive element reachable + usable by Tab/Enter/Space; visible focus ring; logical focus order; no keyboard traps.
- **Labels + alt**: form fields have associated labels; images have meaningful `alt` (empty alt for decorative).
- **Contrast**: text meets WCAG AA (4.5:1 body); don't rely on colour ALONE to convey state.
- **Names + roles**: interactive elements have an accessible name; use ARIA roles/states only when semantics are missing.
- **Respect preferences**: prefers-reduced-motion, zoom to 200% without breakage, responsive to text resize.
- Test with keyboard-only + a screen reader + an automated checker (axe).
