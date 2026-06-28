# codemem — Unified code memory for AIForgeCrew

One module. One read API. One ingest pipeline. Replaces the legacy
`aiforge_memory/` + `aiforge_memory/legacy_context.py` stack.

Spec: `docs/superpowers/specs/2026-04-30-unified-code-memory-design.md`

## What it is

Four-level Neo4j model per repo, plus chunk vectors:

```
   Repo  ─OWNS_SERVICE─►  Service  ─CONTAINS_FILE─►  File_v2 ─DEFINES─►  Symbol_v2
    │                                                  │
    │                                                  └─CHUNKED_AS─► Chunk_v2 (bge-m3 vector)
    │
    ├─ build_cmd, test_cmd, lint_cmd, run_cmd, portforward_cmds
    ├─ runbook_md (LLM-synthesized)
    └─ conventions_md
```

| Level | Answers |
|---|---|
| `Repo`     | "how do I build / test / port-forward this?" |
| `Service`  | "given API X, which services touch it?" (Plan 6 + DEPENDS_ON edges) |
| `File_v2`  | "what does this file do?" (LLM summary, plan 4) |
| `Symbol_v2`| "who calls this method?" (tree-sitter CALLS, plan 3) |
| `Chunk_v2` | "fuzzy NL → top-K files/symbols" (vectors, plan 5) |

## Layered build status

| Layer | What | Plan | Status |
|---|---|---|---|
| **L1** | Repo node + RUNBOOK from RepoMix → LLM | plan 1 | ✅ shipped, 30/30 green Mac+NUC |
| **L2** | Service nodes + OWNS_SERVICE + CONTAINS_FILE; operator override via `.aiforge/services.yaml` | plan 2 | ✅ shipped, 12/12 green Mac+NUC |
| L3 | Per-File summary (LLM, ≤200 tok) + purpose_tags | plan 4 | pending |
| L4 | Symbol_v2 + DEFINES + IMPORTS + CALLS via tree-sitter | plan 3 | pending |
| L5 | Chunk_v2 + bge-m3 embeddings | plan 5 | pending |
| L6 | NL→entities translator (embed + LLM grounding + fastpath) | plan 7 | pending |
| L7 | ContextBundle builder | plan 7 | pending |
| L8 | UnifiedContext rewire + legacy delete | plan 8 | pending |

## Ingest pipeline (declarative, idempotent)

```
  Stage 1   pack_repo         RepoMix → repo_pack.md, sha256
  Stage 2   repo_summary      LLM(qwen3.6-27b) → Repo node + runbook_md
  Stage 3   service_extract   LLM → Service nodes + CONTAINS_FILE edges (Plan 2)
  Stage 4   treesitter_walk   tree-sitter → File_v2 + Symbol_v2 + IMPORTS (Plan 3)
  Stage 5   call_edges        CALLS, EXTENDS, IMPLEMENTS (Plan 3)
  Stage 6   file_summary      LLM per file → File_v2.summary (Plan 4)
  Stage 7   chunk_embed       bge-m3 → Chunk_v2 vectors (Plan 5)
  Stage 8   service_deps      DEPENDS_ON edges (Plan 6)
  Stage 9   learner_writeback ticket outcome → File.summary append (later)
```

Idempotency: stages key on hash. State stored in `~/.aiforge/codemem.state.db` (sqlite). Re-runs that find unchanged hashes are no-ops. Run with `--force` to bypass.

## Operator CLI

```
aiforge-memory doctor                 # check repomix, neo4j, llm
aiforge-memory ingest <repo>          # Stages 1+2+3 (full)
aiforge-memory ingest <repo> --force  # rerun ignoring sha cache
aiforge-memory stats <repo>           # Repo node summary
aiforge-memory services <repo>        # list services + file counts
```

Convenience wrapper for cron: `aiforge-maint codemem ingest <repo>` does the same thing.

## Manual cycle test (per layer)

The pattern: each layer ships with a fixture, an L<N> gate, a README beside it, and a make target. Test order is bottom-up so each layer rests on a green foundation.

### L1 — Repo node

```bash
make test-codemem-L1                            # gated unit + L1 contract test
aiforge-memory ingest tinyrepo \                # real ingest of fixture
    --path aiforge_memory/tests/L1_repo_node/fixtures/tiny_repo
aiforge-memory stats tinyrepo                   # confirm 5 commands + runbook ≥500 chars
```

Expected after ingest:

```json
{"status":"indexed","pack_sha":"<64-hex>","repo":"tinyrepo"}
```

L1 README: `aiforge_memory/tests/L1_repo_node/README.md`

### L2 — Service extract

```bash
make test-codemem-L2
aiforge-memory ingest l2demo \
    --path aiforge_memory/tests/L2_service_extract/fixtures/multi_repo
aiforge-memory services l2demo
```

Expected:

```json
{"repo":"l2demo","services":[
  {"name":"api","role":"api","port":8080,"file_count":3,"source":"llm",...},
  {"name":"worker","role":"consumer","file_count":3,"source":"llm",...}
]}
```

To test operator override, drop `.aiforge/services.yaml` in the repo (see `tests/L2_service_extract/fixtures/services_override.yaml` for shape) and rerun with `--force`. The named service flips to `source:'manual'`.

L2 README: `aiforge_memory/tests/L2_service_extract/README.md`

### L3–L8 (pending plans)

Each layer follows the same pattern: fixture + gate + README + make target + cli command. Add `make test-codemem-L<N>` after the plan ships.

## Coexistence with legacy graphify

During the cutover (Steps 1–9 of the migration plan), legacy `:Symbol` and `:Chunk` nodes from `aiforge_memory/graphify_loader.py` stay live. To keep constraints from clashing, codemem's nodes use `_v2`-suffixed labels: `File_v2`, `Symbol_v2`, `Chunk_v2`. After Step 10 (legacy delete), a single migration drops the suffix.

`Repo` and `Service` are unsuffixed: codemem owns them outright (graphify never modeled services).

## Schema-version stamp

Every codemem node has `schema_version: 'codemem-v1'`. Useful for:
- targeted rollback (`MATCH (n) WHERE n.schema_version='codemem-v1' DETACH DELETE n`)
- forward migrations when the model changes (bump to `codemem-v2`, write migration script)

## Storage layout

```
~/.aiforge/
  codemem.state.db          sqlite — merkle hashes, services overrides, query cache
  runtime.env               shared by aiforge runtime; AIFORGE_CODEMEM_LM_MODEL etc.
```

Override env vars:

```
AIFORGE_NEO4J_URI            bolt://127.0.0.1:7687  (default)
AIFORGE_NEO4J_USER           neo4j
AIFORGE_NEO4J_PASSWORD       password
AIFORGE_CODEMEM_LM_URL       http://127.0.0.1:1235/v1  (LM Studio)
AIFORGE_CODEMEM_LM_MODEL     qwen3.6-27b-instruct (or model id)
AIFORGE_CODEMEM_LM_KEY       lm-studio
AIFORGE_CODEMEM_REPOMIX      /path/to/repomix (default: 'repomix' on PATH)
AIFORGE_CODEMEM_STATE_DB     ~/.aiforge/codemem.state.db (default)
```

## Read API (final shape — not yet wired)

```python
from aiforge_memory.api import bundle

ctx = bundle.query(
    text="fix payment processing in posClientBackend",
    role="doer",
    repo_hint="PosClientBackend",
    token_budget=4000,
)
prompt_section = ctx.render()  # markdown block ready to drop into LLM input
```

Sources merged in priority order (lands in plan 7+8):

1. Service runbook (build/test/portforward)              [hard]
2. Anchor file summaries (≤200 tok each, top 8)          [hard]
3. Symbol signatures + call neighbours (top 12)
4. Aider RepoMap fragment (focal_files = anchor.files)
5. Past tickets touching anchor files (Postgres join)
6. T3 recipes (Memory)
7. Repo runbook tail
8. Operator memory hits (`~/.claude/memory/`)

`UnifiedContext.for_*()` becomes a 5-line wrapper around `bundle.query`.

## Failure mode summary

Spec §10 has the full table. Quick read:

- Stage 1 binary missing → `RepoMixNotFound`, ingest aborts cleanly.
- Stage 2/3 LLM bad JSON → one retry with stricter prompt, then `RepoSummaryError` / `ServiceExtractError`. Old graph state served in the meantime.
- Stage 3 hallucinated paths → silently dropped.
- Cypher / Neo4j down → ingest aborts with traceback (run `doctor`); existing graph untouched.
- Bundle empty → `errors=['no_anchors']` set; agent prompt unchanged but flagged.

## Running the full suite

```bash
make test-codemem-all        # all layer gates currently shipped
make test-codemem-L1         # plan 1 only
make test-codemem-L2         # plan 2 only
```

Tunneled local dev: SSH-forward NUC's Neo4j to localhost so live tests run from your laptop:

```bash
ssh -fNT -L 7687:127.0.0.1:7687 mani@192.168.70.191
AIFORGE_NEO4J_URI=bolt://127.0.0.1:7687 .venv/bin/pytest aiforge_memory/tests/ -v
```
