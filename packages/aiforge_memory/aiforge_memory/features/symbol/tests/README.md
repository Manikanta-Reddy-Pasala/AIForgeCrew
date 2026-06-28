# Layer L4 — Symbol_v2 + call edges gate

## Purpose
After Stage 4 + Stage 5 of ingest, every source file has been parsed
with tree-sitter, every class/method/function exists as a `Symbol_v2`
under the right `(:File_v2)`, IMPORTS edges connect files, and CALLS
edges connect callers to callees with a confidence tier.

## Fixture
- `fixtures/poly_repo/` — 5 source files across Python, Java, TypeScript:
  - `api/main.py` (PaymentService class + 3 methods + 2 top-level fns)
  - `api/helpers.py` (single function `normalize`)
  - `svc/Service.java` (PaymentService class + ctor + 2 methods)
  - `web/main.ts` (Counter class + 2 methods + bootstrap fn)
  - `web/helpers.ts` (single function `normalize`)

## Command

    make test-codemem-L4

or directly:

    pytest aiforge_memory/tests/L4_symbols/ -v

## Pass criteria
- `walk_repo` returns 5 WalkedFile entries; 0 parse errors
- Python: PaymentService class + 3 methods + 2 top-level functions emitted
- Java: PaymentService class + ctor + 2 methods emitted
- TypeScript: Counter class + 2 methods + 1 top-level function emitted
- IMPORTS edges resolve `from .helpers import normalize` to `api/helpers.py`
- CALLS edges:
  - same-file high-confidence (1.0): `bootstrap → Counter.increment`
  - import-aware (0.7): `PaymentService.process → api/helpers.py::normalize`
  - Java intra-class: `PaymentService.process → PaymentService.normalize`
- No self-CALLS (`caller != callee`)
- Symbol_v2 unique constraint enforces (repo, fqname)

## Sample expected output

After ingest with `--force`:

    {
      "status":"indexed",
      "files_count":5,
      "symbols_count":13,
      "imports_count":3,
      "calls_count":7,
      ...
    }

Cypher inspect:

    MATCH (s:Symbol_v2 {repo:'poly_test'})-[c:CALLS]->(t:Symbol_v2)
    RETURN s.fqname, t.fqname, c.confidence ORDER BY c.confidence DESC

## On failure
- Tree-sitter import error → `pip install tree-sitter tree-sitter-language-pack`
- Empty IMPORTS for Python relatives → check `python.scm` includes
  `relative_import` capture (Python rev > 0.20 changes node names)
- 0 CALLS edges → check that the file actually has method invocations
  (test fixtures sometimes optimize away simple expressions)
- Self-loops → `_enclosing_symbol` returning the same fqname; verify
  symbol line ranges don't overlap by mistake
- escalation: open ticket `CODEMEM-L4-<short>`
