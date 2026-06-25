---
name: typescript-practices
description: Use TypeScript's type system to prevent bugs, not fight it
triggers: [typescript, ts, types, interface, generic, tsconfig, type safety]
source: builtin
---

- **`strict: true`.** noImplicitAny, strictNullChecks — the whole point is catching null/undefined and shape errors at compile time.
- **Model the domain in types**: discriminated unions for variants, `readonly` where applicable, literal types over loose strings.
- **Avoid `any`** — it disables checking and spreads. Use `unknown` + narrowing at boundaries (parsed JSON, API responses).
- **Type the boundaries**: validate/parse external input (zod or hand-written guards) so the rest of the code trusts its types.
- **Let inference work**; annotate function signatures + public APIs, not every local.
- **Don't lie with assertions** (`as`) — a wrong cast is a runtime bug the compiler trusted you on.
- Narrow with `if (x == null)`, `typeof`, `in`, discriminants — not `!` everywhere.
