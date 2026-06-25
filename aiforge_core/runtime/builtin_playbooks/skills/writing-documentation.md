---
name: writing-documentation
description: Write docs and comments that stay useful and don't lie
triggers: [documentation, docs, readme, comment, docstring, adr, explain]
source: builtin
---

- **Document WHY, not WHAT.** The code says what; comments explain intent, trade-offs, gotchas, and non-obvious constraints.
- **README answers**: what is this, how do I run/build/test it, where do things live. Keep the golden-path commands correct.
- **Public APIs get docstrings**: purpose, params, return, raises, one example.
- **Keep docs next to code** and update them in the SAME change — stale docs are worse than none.
- **Don't comment the obvious** (`i++  // increment i`); delete redundant comments, they rot.
- **Decisions → ADR** (see `architecture-decision` workflow) so future readers know why, not just what.
- Prefer a runnable example/test over prose where it's clearer.
