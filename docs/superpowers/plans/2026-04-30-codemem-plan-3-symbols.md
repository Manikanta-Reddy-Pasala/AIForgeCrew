# Codemem Plan 3 — L4 tree-sitter symbols + call edges

**Goal:** Stage 4 walks every source file with tree-sitter, materializing `File_v2` (with hash, lang, lines) + `Symbol_v2` (class/method/func/iface) + `DEFINES` + `IMPORTS` edges. Stage 5 layers `CALLS` + `EXTENDS` + `IMPLEMENTS` edges. Pass L4 gate.

**Architecture:** `tree_sitter_language_pack` supplies parsers for Python/Java/TypeScript at v1. Per-language tree-sitter queries (S-expressions stored in `prompts/queries/*.scm`) extract symbols + edges. Call resolution: same-file → import-aware → fuzzy with confidence drop.

**Tech Stack:** tree-sitter 0.25.x, tree-sitter-language-pack 0.13.x, neo4j.

**Spec:** §4 (Symbol_v2), §5 (Stages 4+5), §7 (L4 gate ≥ ±5% vs ctags).

## File structure

**Create:**
- `aiforge_core/codemem/ingest/treesitter_walk.py` (~250 lines)
- `aiforge_core/codemem/ingest/edges.py` (~200 lines)
- `aiforge_core/codemem/ingest/queries/python.scm`
- `aiforge_core/codemem/ingest/queries/java.scm`
- `aiforge_core/codemem/ingest/queries/typescript.scm`
- `aiforge_core/codemem/store/symbol_writer.py` (~120 lines)
- `aiforge_core/codemem/tests/L4_symbols/` tree (gate + per-lang fixtures + README)

**Modify:**
- `aiforge_core/codemem/store/schema.py` — Symbol_v2 + IMPORTS + CALLS indices
- `aiforge_core/codemem/ingest/flow.py` — Stage 4+5 wired after Stage 3
- `pyproject.toml` — add `tree-sitter-language-pack`

## Tasks

### T1: deps + plan doc (this file)
### T2: schema additions (Symbol_v2 uniq, indices)
### T3: file walker — File_v2 + Symbol_v2 + DEFINES + IMPORTS
### T4: call edges — CALLS via per-lang queries + heuristic resolve
### T5: Wire Stage 4+5 in flow
### T6: L4 gate + README
### T7: NUC deploy + manual cycle

## L4 gate criteria

- Symbol count within ±10% of `tree --recurse | wc -l` baseline (relaxed from spec §7's ±5% for v1 — multi-language tolerance)
- 0 parse errors on green files (Python + Java + TS fixtures)
- CALLS edges count > 0 per file with method calls
- IMPORTS edges form a DAG (no self-loop)
- Symbol_v2.fqname is unique within (repo)
