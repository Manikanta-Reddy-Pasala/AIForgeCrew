# repo — tests

Covers `pack_repo.pack()` (RepoMix → pack text + sha256), `extract.summarize()`
(pack → LLM repo summary) and `core/state.py`, the sqlite merkle tables that
make a re-ingest of unchanged content a no-op.

## Fixtures

- `fixtures/tiny_repo/` — toy repo (README, Makefile, `src/main.py`), real
  enough that RepoMix produces non-trivial output
- `fixtures/tiny_pack.md` — recorded pack, used when RepoMix is mocked
- `fixtures/llm_response_ok.json` — recorded LLM reply
- `expected/tiny_repo_node.json` — expected summary fields

## Run

    pytest aiforge_memory/features/repo/tests -v

## On failure

- `repomix` not on PATH → `npm i -g repomix`, or set `AIFORGE_CODEMEM_REPOMIX`.
  Only the `live_repomix` case in `test_pack_repo.py` needs it; the rest mock it.
- LLM 4xx on `response_format` → LM Studio rejects `json_object`. `llm_compat`
  defaults to `json_schema`; override with `AIFORGE_CODEMEM_RESPONSE_FORMAT`.
- Summary truncated → `AIFORGE_CODEMEM_PACK_MAX_CHARS` and
  `AIFORGE_CODEMEM_REPO_SUMMARY_MAX_TOKENS`.
