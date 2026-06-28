# Layer L3 — Per-file summary gate

## Purpose
After Stage 6, every File_v2 with size ≤ 32 KB and parse_error=false has
`summary` (non-empty, ≤200 token) and `purpose_tags` (3–5 kebab-case)
written by the planner LLM. Skipped files carry a `skipped_reason` value.

## Fixture
- input: `aiforge_memory/tests/L4_symbols/fixtures/poly_repo/`
  (5 files, all small enough to summarize)
- recorded LLM responses are inline in the test (no JSON fixture file).

## Command

    make test-codemem-L3

or directly:

    pytest aiforge_memory/tests/L3_file_summary/ -v

## Pass criteria
- `summarize_files` returns one entry per WalkedFile
- Skip rules respected (parse_error → "parse_error", size > MAX → "too_large", LLM throw → "llm_error")
- Bad JSON retries once with stricter prompt
- `write_summaries` updates File_v2 only — never creates new nodes
- Gate (with real Neo4j): summary, purpose_tags populated for ≥1 file in poly_repo

## On failure
- LLM unreachable → real run can use `skip_summaries=True` to bypass
- Skip count high but updated count low → tweak `AIFORGE_CODEMEM_FILE_SUMMARY_MAX_BYTES`
- Tags arrive as null → check that the LLM response includes `purpose_tags` array
- escalation: open ticket `CODEMEM-L3-<short>`
