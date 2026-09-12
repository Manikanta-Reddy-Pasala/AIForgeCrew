# service — tests

Covers `extract.extract_services()`: pack plus file list → service records, with
`.aiforge/services.yaml` operator overrides beating the LLM and hallucinated
file paths dropped rather than trusted.

## Fixtures

- `fixtures/multi_repo/` — two services (`api/` on :8080, `worker/` consuming
  NATS), six source files
- `fixtures/llm_services_ok.json` — recorded LLM reply
- `fixtures/services_override.yaml` — operator override sample
- `expected/services.json` — expected service summary

## Run

    pytest aiforge_memory/features/service/tests -v

## On failure

- Zero services returned → the pack was truncated before the LLM saw the
  services; check `AIFORGE_CODEMEM_PACK_MAX_CHARS`, then re-record
  `llm_services_ok.json` from a clean run.
- Override ignored → `.aiforge/services.yaml` must sit at the root of the repo
  path passed to `extract_services`.
- Hallucinated paths reaching the output → file validation resolves against the
  repo path; confirm it is absolute.
