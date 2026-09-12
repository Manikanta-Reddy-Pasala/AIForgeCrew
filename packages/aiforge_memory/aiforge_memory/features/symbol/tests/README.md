# symbol — tests

Covers the tree-sitter walk (`extract.walk_repo`), call and import edge
resolution (`extract_calls`, including the source-aware variants) and LLM symbol
summaries (`summarise`).

## Fixture

`fixtures/poly_repo/` — a small multi-language tree with `api/` (Python),
`svc/` (Java) and `web/` (TypeScript), so one walk exercises three grammars.
Tag queries live in `../queries/`.

## Run

    pytest aiforge_memory/features/symbol/tests -v

## On failure

- tree-sitter import error → reinstall `tree-sitter` and
  `tree-sitter-language-pack`. The pin is `>=0.13,<1.0`: the 1.6.x wheel ships
  dist-info with no module.
- No import edges for Python relative imports → check the `relative_import`
  capture in the Python tag query; node names moved after tree-sitter 0.20.
- Self-referencing CALLS edges → overlapping symbol line ranges make the
  enclosing-symbol lookup return the caller itself.
- Summaries empty, slow or rate-limited → the `AIFORGE_SYMSUM_*` knobs
  (concurrency, throttle, timeout, retries, head/tail lines, kinds).
