# Layer L1 — Repo node ingest gate

## Purpose
After Stage 1 (RepoMix pack) and Stage 2 (LLM repo summary), a single
`(:Repo {name})` exists in Neo4j with all four core commands populated,
a runbook ≥500 chars (relaxed to ≥200 for the tiny_repo fixture), and a
stable pack_sha that idempotency depends on.

## Fixture
- input: `fixtures/tiny_repo/` (3 files; README, Makefile, src/main.py)
- recorded pack: `fixtures/tiny_pack.md` (used when LLM/RepoMix mocked)
- recorded LLM response: `fixtures/llm_response_ok.json`
- expected node properties: `expected/tiny_repo_node.json`

## Command

    make test-codemem-L1

or directly:

    pytest aiforge_memory/tests/L1_repo_node/ -v

## Pass criteria
- `(:Repo {name:'tiny_repo_test'})` exists after ingest
- 5/5 fields populated: lang_primary, build_cmd, test_cmd, run_cmd, portforward_cmds
- `runbook_md` length ≥ 200 characters (tiny fixture; production repos hit ≥500)
- `last_pack_sha` matches the recorded sha
- `last_indexed_at` is non-null (Neo4j DateTime)
- Re-running ingest with the same content sets `status='skipped_unchanged'`
- `--force` re-runs and overwrites `last_indexed_at`

## Sample expected output

After `aiforge-memory ingest tiny_repo_test --path .../tiny_repo`:

    {
      "status": "indexed",
      "pack_sha": "<64-hex>",
      "repo": "tiny_repo_test"
    }

Repo node in Neo4j (abbreviated):

    name: "tiny_repo_test"
    lang_primary: "python"
    build_cmd: "make build"
    test_cmd: "make test"
    run_cmd: "make run"
    portforward_cmds: ["kubectl port-forward svc/tiny-repo 8080:8080 -n default"]
    runbook_md: "## Tiny Repo Runbook ..."
    last_pack_sha: "<sha256>"
    schema_version: "codemem-v1"

## On failure
- `repomix` not on PATH → install with `npm i -g repomix` or set
  `AIFORGE_CODEMEM_REPOMIX=/path/to/repomix`. Unit tests skip with
  `live_repomix` marker; the gate uses mocks, so this only matters
  for `aiforge-memory doctor`.
- Neo4j unreachable → check `AIFORGE_NEO4J_URI`/`USER`/`PASSWORD`,
  ensure bolt port (7687) is open, run `cypher-shell` manually.
- LLM 4xx — `response_format={"type":"json_object"}` may not be
  honored by every LM Studio build; the parser tolerates fenced
  output, but if both mocked and live responses are bad JSON,
  re-record `fixtures/llm_response_ok.json` with a clean run.
- escalation: open ticket `CODEMEM-L1-<short>`.
