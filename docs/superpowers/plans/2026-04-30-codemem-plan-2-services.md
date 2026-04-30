# Codemem Plan 2 — L2 Service extract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stage 3 of ingest — LLM extracts services from the RepoMix pack, operator override merges in, Service + OWNS_SERVICE + CONTAINS_FILE materialize in Neo4j. Pass L2 gate.

**Architecture:** New module `codemem/ingest/service_extract.py` calls qwen3.6-27b with the pack and a strict-JSON prompt that yields `[{name, description, role, tech_stack, port, files[]}]`. `.aiforge/services.yaml` operator override merges (operator wins per-name). `codemem/store/service_writer.py` upserts via MERGE. Flow runs Stage 3 after Stage 2.

**Tech Stack:** Python 3.11, neo4j, openai SDK, PyYAML for override file.

**Spec:** `docs/superpowers/specs/2026-04-30-unified-code-memory-design.md` §4 (Service node), §5 (Stage 3), §7 (L2 gate).

## File structure

**Create:**
- `aiforge_core/codemem/ingest/service_extract.py` (~150 lines)
- `aiforge_core/codemem/ingest/prompts/service_extract.txt`
- `aiforge_core/codemem/store/service_writer.py` (~90 lines)
- `aiforge_core/codemem/tests/L2_service_extract/__init__.py`
- `aiforge_core/codemem/tests/L2_service_extract/README.md`
- `aiforge_core/codemem/tests/L2_service_extract/test_service_extract.py`
- `aiforge_core/codemem/tests/L2_service_extract/test_service_writer.py`
- `aiforge_core/codemem/tests/L2_service_extract/test_l2_gate.py`
- `aiforge_core/codemem/tests/L2_service_extract/fixtures/multi_repo/{api,worker}/...`
- `aiforge_core/codemem/tests/L2_service_extract/fixtures/llm_services_ok.json`
- `aiforge_core/codemem/tests/L2_service_extract/fixtures/services_override.yaml`
- `aiforge_core/codemem/tests/L2_service_extract/expected/services.json`

**Modify:**
- `aiforge_core/codemem/store/schema.py` — Service constraint + index
- `aiforge_core/codemem/ingest/flow.py` — wire Stage 3 after Stage 2
- `aiforge_core/codemem/api/cli.py` — `services` subcommand
- `pyproject.toml` — add `pyyaml` dep

## Tasks

### Task 1: Schema additions for Service

- Add `Service` uniqueness on (repo, name), B-tree on (repo, role)
- Add unit test ensuring constraint exists

### Task 2: multi_repo fixture

- 2 logical services in one repo: `api/` (FastAPI service) + `worker/` (NATS consumer)
- Total ~6 files, real enough that LLM can name them

### Task 3: service_extract.py + prompt

- Strict-JSON: `{"services":[{"name":..., "role":..., "tech_stack":[...], "port":..., "files":[...]}]}`
- One retry on bad JSON
- Validate file paths exist in repo (silently drop hallucinations)

### Task 4: services.yaml override merge

- Read `.aiforge/services.yaml` if exists
- Operator entry by name wins (full replace per service)
- Source field: `'manual'` if from yaml, `'llm'` otherwise

### Task 5: service_writer.py

- `upsert_services(driver, repo, services)`:
  - MERGE Service {repo, name}; SET props
  - MERGE (Repo)-[:OWNS_SERVICE]->(Service)
  - MERGE (Service)-[:CONTAINS_FILE]->(File {repo, path}) — File node placeholder created if missing (actual File props populate in plan 3)

### Task 6: Wire Stage 3 in flow.py

- `flow.ingest_repo` runs Stage 3 after Stage 2
- IngestResult adds `services_count` field

### Task 7: CLI services subcommand

- `aiforge-codemem services <repo>` — list services with file counts

### Task 8: L2 gate

- Run flow on multi_repo (mocked LLM with fixture response)
- Assert: 2 Service nodes, OWNS_SERVICE edges = 2, CONTAINS_FILE edges = 6, override-set name has source='manual'

### Task 9: Deploy + NUC verify

- Push, NUC pull, run gate live, smoke ingest with real LLM
