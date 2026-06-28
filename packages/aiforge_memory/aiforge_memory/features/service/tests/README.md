# Layer L2 — Service extract gate

## Purpose
After Stage 3, the right number of `(:Service)` nodes exists for the
repo. Each service has `(repo, name)` uniqueness, a non-empty
`tech_stack`, a real `role`, and `CONTAINS_FILE` edges only to files
that exist on disk. The parent `(:Repo)` has one `OWNS_SERVICE` edge
per service. Operator overrides via `.aiforge/services.yaml` win and
are tagged `source='manual'`.

## Fixture
- input: `fixtures/multi_repo/` (api/ + worker/, 6 source files total)
- recorded LLM response: `fixtures/llm_services_ok.json`
- override sample: `fixtures/services_override.yaml`
- expected service summary: `expected/services.json`

## Command

    make test-codemem-L2

or directly:

    pytest aiforge_memory/tests/L2_service_extract/ -v

## Pass criteria
- 2 Service nodes exist for the test repo
- 2 OWNS_SERVICE edges from the Repo
- 6 CONTAINS_FILE edges total (3 per service)
- `api.port == 8080`; `api.role == 'api'`; `worker.role == 'consumer'`
- Override scenario: `api.source == 'manual'`, `worker.source == 'llm'`
- Hallucinated paths in LLM output are silently dropped
- `aiforge-memory services <repo>` prints both services with file_count

## Sample expected output

After ingest:

    {
      "status": "indexed",
      "pack_sha": "<64-hex>",
      "repo": "l2_smoke",
      "services_count": 2,
      "file_edges_count": 6
    }

`aiforge-memory services l2_smoke`:

    {
      "repo": "l2_smoke",
      "services": [
        {"name":"api","role":"api","port":8080,"source":"llm","file_count":3,...},
        {"name":"worker","role":"consumer","port":null,"source":"llm","file_count":3,...}
      ]
    }

## On failure
- LLM returns 0 services → check pack truncation (`max_input_chars`),
  re-record `llm_services_ok.json` with a clean run
- Hallucinated paths show up in graph → check `_validate_files`
  is using absolute paths under `repo_path`
- Override not honored → confirm `.aiforge/services.yaml` is at the
  repo root the test passes to `extract_services`
- Stale services from previous runs → `service_writer._PRUNE_STALE_SERVICES`
  is the gate; verify Cypher counters in `counts['pruned_services']`
- escalation: open ticket `CODEMEM-L2-<short>`
