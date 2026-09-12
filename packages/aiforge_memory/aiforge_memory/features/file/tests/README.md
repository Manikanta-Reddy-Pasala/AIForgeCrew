# file — tests

Covers `extract.summarize_files()`: one entry per walked file, a recorded
`skipped_reason` for each file it declines (parse error, too large, LLM error),
and a single stricter retry when the model returns unparseable JSON.

## Fixture

The shared `../../symbol/tests/fixtures/poly_repo/` tree (Python, Java,
TypeScript), whose files are all small enough to summarize. Recorded LLM
responses are inline in the test, not a JSON fixture.

## Run

    pytest aiforge_memory/features/file/tests -v

## On failure

- Everything skipped as too large → `AIFORGE_CODEMEM_FILE_SUMMARY_MAX_BYTES`
  (default 32768).
- LLM unreachable → `AIFORGE_CODEMEM_LM_URL` / `_MODEL` / `_KEY`.
- Tags arrive null → the model dropped `purpose_tags` from its reply; check the
  `response_format` in use (`AIFORGE_CODEMEM_RESPONSE_FORMAT`).
