# Unified Code Memory — Design Spec

**Date:** 2026-04-30
**Status:** Draft (brainstorm-approved, awaiting implementation plan)
**Owner:** AIForgeCrew (Manikanta)
**Module:** `aiforge_core/codemem/` (new)

## 1. Problem

Today's code-context surface (`aiforge_core/index/` + `aiforge_core/memory/`, ~4350 LOC) wraps Aider RepoMap, Graphify, tree-sitter ingest, repo_learn, repo_notes, symbol_embed and an 8-source UnifiedContext aggregator. Operationally it works, but on real queries it fails the agent in five distinct ways:

| ID | Pain |
|---|---|
| A | Given an API/method, the system cannot reliably enumerate the methods or services that exercise it. Call-graph traversal is built but not exposed end-to-end through the query path. |
| B | Single-repo mapping itself is unreliable, before any cross-repo concern. |
| C | "How do I build / test / port-forward this repo?" has no canonical answer surface. |
| D | The learner produces T3 facts that are written but rarely re-used at inference. The learner output is not surfaced as the per-file or per-repo doc that operators want. |
| E | Per-file purpose is rediscovered by the LLM every ticket. Past tickets touching a file are not joined into context. |
| F | The eight stitched sources feel "high tech, no use" — surface area is large, observable behaviour is unreliable. |

The system needs consolidation, not augmentation.

## 2. Goals & non-goals

### Goals

1. One module, one read API, that any agent (chat / planner / doer / intent) can call to get a token-budgeted, structured `ContextBundle` for a natural-language query.
2. Four-level canonical model — `Repo` → `Service` → `File` → `Symbol`, plus separate `Chunk` vectors — that maps directly to the five real query shapes (API → services, file → purpose, repo → runbook, query → focal files, ticket → past work).
3. Incremental, declarative ingestion pipeline with idempotent stages and per-stage observability.
4. Each layer ships with a golden-test gate and a README beside the test that documents the contract.
5. Layer-by-layer cutover from the legacy stack with feature-flag rollback at every step.

### Non-goals (this spec)

- Cross-repo edges (`CALLS_REPO`). Edge label is reserved in the schema; population is a follow-up spec.
- Replacing Postgres for tickets/events/costs. Postgres remains; only code memory moves.
- Multi-tenant isolation. Single-operator footprint stays.
- A new chat surface. Chat keeps its existing `do_unified_memory_query` tool entry-point; only what's behind the call changes.
- Replacing the Aider RepoMap entirely. RepoMap is kept as a re-rank input inside the bundle, not as a primary index — 922d828's A/B verdict says it wins on real queries today and should not be discarded.

## 3. High-level architecture

```
┌─────────── aiforge_core/codemem ───────────────────────────────┐
│                                                                │
│  ingest/  (CocoIndex flow, incremental, FS-watching)           │
│    pack_repo.py         RepoMix dump → repo_pack.md            │
│    repo_summary.py      LLM(repo_pack) → Repo node + RUNBOOK   │
│    service_extract.py   LLM(repo_pack) → services.yaml draft   │
│    treesitter_walk.py   tree-sitter → File + Symbol nodes      │
│    edges.py             CALLS / IMPORTS / EXTENDS / OWNS_*     │
│    embed.py             bge-m3 → Chunk vectors                 │
│    learner_writeback.py ticket outcome → File.summary patch    │
│                                                                │
│  store/                                                        │
│    schema.py            Neo4j constraints + indices            │
│                                                                │
│  query/                                                        │
│    translator.py        NL → entities (D: embed + LLM ground)  │
│    cypher.py            parameterised templates                │
│    fastpath.py          regex bypass for Class.method / TICKET │
│                                                                │
│  api/                                                          │
│    bundle.py            ContextBundle builder                  │
│    cli.py               aiforge codemem {ingest|query|stats}   │
│                                                                │
│  tests/                                                        │
│    L1_repo_node/  L2_service_extract/  L3_file_summary/        │
│    L4_symbols/    L5_chunks_vectors/   L6_translator/          │
│    L7_bundle/     L8_e2e/              (each with README.md)   │
└────────────────────────────────────────────────────────────────┘
       ▲                                                ▲
       │                                                │
   PosClientBackend                              user query (NL)
   (and other repos)                                    │
                                                        ▼
                                  UnifiedContext.for_*()  (5-line glue)
```

### Key shifts versus current code

- **One module** (`codemem/`) replaces `index/` plus `memory/code_context.py` plus the code-facing parts of `memory/unified_query.py`. The old `memory/` package keeps T1–T3 facts only (episodes, tickets, patterns).
- **CocoIndex** drives ingestion as a single declarative flow. It replaces six standalone ingest scripts.
- **Neo4j** stays as the primary store; Postgres stays for tickets.
- **Aider RepoMap** is kept as one signal inside the bundle (re-rank input), not as a primary index.
- **Graphify** is deprecated — tree-sitter ingest does the same job natively in CocoIndex. One less moving part.
- **RepoMix** is one ingredient (Stage 1 of ingest), not the whole pipeline.

## 4. Data model (Neo4j)

### Nodes

| Label | Key | Properties |
|---|---|---|
| `Repo` | `name` | path, lang_primary, build_cmd, test_cmd, lint_cmd, run_cmd, portforward_cmds[], conventions_md, runbook_md, last_pack_sha, last_indexed_at |
| `Service` | `(repo, name)` | description, role (api/consumer/scheduler/...), tech_stack[], port, source ('llm'/'manual'/'cluster'), confidence |
| `File` | `(repo, path)` | hash (merkle), lang, lines, summary (LLM, ≤200 tok), purpose_tags[], indexed_at, last_ticket_touched |
| `Symbol` | `(repo, fqname)` | kind (class/method/func/iface), file_path, signature, line_start, line_end, doc_first_line |
| `Chunk` | `id` | repo, file_path, symbol_fqname?, text, embed_vec (bge-m3, 1024d), token_count |

### Edges

| From | To | Edge | Use |
|---|---|---|---|
| Repo | Service | `OWNS_SERVICE` | repo→service membership |
| Service | File | `CONTAINS_FILE` | service→file (m:n; a config file may belong to >1 service) |
| File | Symbol | `DEFINES` | file→symbol |
| Symbol | Symbol | `CALLS` | tree-sitter call graph |
| Symbol | Symbol | `EXTENDS` / `IMPLEMENTS` | inheritance |
| File | File | `IMPORTS` | import graph |
| File | Chunk | `CHUNKED_AS` | file → chunks (vector recall) |
| Symbol | Chunk | `CHUNKED_AS` | symbol-level chunks (separate from file chunks) |
| Service | Service | `DEPENDS_ON` | derived from cross-service IMPORTS / NATS subjects / HTTP calls |
| Repo | Repo | `CALLS_REPO` | reserved; populated in follow-up spec |

### Indices

- Unique constraints: `Repo.name`, `(Service.repo, Service.name)`, `(File.repo, File.path)`, `(Symbol.repo, Symbol.fqname)`
- Vector index: `Chunk.embed_vec` (1024d, cosine)
- Fulltext: `File.summary`, `Symbol.signature`, `Repo.runbook_md`
- B-tree: `File.last_ticket_touched`, `Symbol.kind`

### How the model maps to pains

- **Pain A:** `MATCH (s:Service)-[:CONTAINS_FILE]->(f:File)-[:DEFINES]->(:Symbol {fqname:'X'}) RETURN s` then 1–2 hops over `DEPENDS_ON`.
- **Pain C:** `MATCH (r:Repo {name:'X'}) RETURN r.runbook_md, r.portforward_cmds`.
- **Pain D:** Stage-9 learner writes append into `File.summary` with provenance, becoming first-class context.
- **Pain E:** `File.last_ticket_touched` joined to Postgres `tickets` returns past tickets per anchor file.

## 5. Ingestion pipeline (CocoIndex)

Trigger: `aiforge codemem ingest <repo>` (full); FS watcher (incremental, dev mode); post-merge git hook (CI).

```
  Stage 1  pack_repo        RepoMix dump          repo_pack.md, last_pack_sha
  Stage 2  repo_summary     LLM(qwen3.6-27b)      Repo node, runbook_md
  Stage 3  service_extract  LLM(qwen3.6-27b)      services.yaml draft → Service nodes
  Stage 4  treesitter_walk  tree-sitter           File + Symbol + IMPORTS
  Stage 5  call_edges       tree-sitter queries   CALLS, EXTENDS, IMPLEMENTS
  Stage 6  file_summary     LLM(qwen3-coder)      File.summary, File.purpose_tags
  Stage 7  chunk_embed      bge-m3 sidecar :8764  Chunk + CHUNKED_AS
  Stage 8  service_deps     Cypher + LLM verify   DEPENDS_ON
  Stage 9  learner_writeback (post-ticket hook)   File.summary append, last_ticket_touched
```

### Stage details

**Idempotency.** Every stage keys on a content hash. CocoIndex stores stage state in `~/.aiforge/codemem.state.db` (sqlite). Re-running a stage is a no-op when nothing changed.

**Operator overrides.** Stage 3 writes a draft `services.yaml`; if `.aiforge/services.yaml` exists, the operator override wins and CocoIndex preserves it across re-ingest.

**Failure isolation.** Each stage writes inside a transaction; on error it rolls back and the previous good state is served by reads. Stage failures are logged to `Repo.last_error` (or `File.last_error` for per-file stages) and counted via `hooks.emit_step`.

**Cost (PosClientBackend, ~800 files, 80K LOC, first run):**

| Stage | Cost |
|---|---|
| 1 | ~10 s (RepoMix) |
| 2 | 1 LLM @ qwen3.6 ~15 s |
| 3 | 1 LLM @ qwen3.6 ~15 s |
| 4–5 | ~2 min (tree-sitter, parallel) |
| 6 | 800 LLM @ qwen3-coder ~15 min (skip-cached after first) |
| 7 | ~3 min (bge-m3, batched) |
| 8 | ~30 s |
| **Total first run** | ~22 min |
| Incremental update on a 5-file PR | ~30 s |

## 6. Query path

**Entry:**
```python
codemem.query(text, *, role, repo_hint=None, token_budget=4000) -> ContextBundle
```

```
user text ──► fastpath?
              ├─ regex hit (Class.method | TICKET-123 | path/to/file.ext)
              │       → direct Neo4j MERGE → skip translator → Stage R
              │
              └─ no hit → Stage T (translator)


  Stage T  translate
    T1) embed query (bge-m3) → top-K Chunk vectors → candidate File + Symbol set (k=20)
    T2) load Service catalog from Neo4j (cheap)
    T3) LLM (qwen3.6-27b, JSON-strict):
          input  : {query, services[], top_files[], top_symbols[]}
          output : {intent, services[], files[], symbols[], hops:1|2, keywords[]}
    T4) validate: every returned name MUST exist in candidates;
          drop hallucinations silently, log to bundle.errors

  Stage R  retrieve (parameterised Cypher)
    R1) anchors  = grounded {services, files, symbols}
    R2) traversal:
          (anchor:Symbol)-[:CALLS|CALLED_BY*1..hops]-(s)
          (anchor:File)-[:IMPORTS*1..hops]-(f)
          (anchor:Service)-[:DEPENDS_ON*1..hops]-(svc)
    R3) widen: group by service; pick service runbook + commands
    R4) ticket history: Postgres `tickets WHERE files_touched ∈ anchor.files` LIMIT 5
    R5) T3 recipes: Neo4j (:Pattern) WHERE keywords ∩ query.keywords

  Stage B  bundle (token-budget pack, priority drop)
    1. Service runbook (build/test/portforward)         [hard]
    2. Anchor file summaries (≤200 tok each, top 8)     [hard]
    3. Symbol signatures + call neighbours (top 12)
    4. Aider RepoMap fragment (focal_files = anchor.files)
    5. Ticket history (top 5)
    6. T3 recipes (top 3)
    7. Repo runbook tail
    8. Operator memory hits
    → ContextBundle.render() (existing dataclass, extended)
```

**Latency budget:** embed 50 ms + catalog 20 ms + LLM 2 s + Cypher ~150 ms + bundle pack 30 ms = **~2.3 s end-to-end**. Fastpath: ~250 ms (no LLM).

**Caching:** identical `(text, repo_hint, role)` bundle cached 5 min in-process (LRU 256). Invalidated on `last_indexed_at` change for any covered repo.

**Read-API surface.** `UnifiedContext.for_chat / for_planner / for_doer` becomes a 5-line wrapper that calls `codemem.query` then merges the non-code sources (operator memory, claude_memory, repo doc tail). Sources 1–3 of today's UnifiedContext collapse into `codemem.query`; sources 4–8 stay as thin merges.

## 7. Test gates per layer

Every test has a `README.md` beside it. The README is the contract; the test code is the executor. Tests run locally via `make test-codemem-L<N>`; full suite via `make test-codemem-all`.

```
aiforge_core/codemem/tests/
  README.md                        (index)
  L1_repo_node/                    test + README + fixtures + expected
  L2_service_extract/              ...
  L3_file_summary/
  L4_symbols/
  L5_chunks_vectors/
  L6_translator/
  L7_bundle/
  L8_e2e/
```

| Layer | Gate | Pass criterion | Blocks |
|---|---|---|---|
| L1 Repo | extract build/test/run/portforward from RepoMix pack | 5/5 commands populated, runbook_md ≥500 chars, schema-valid | L2 |
| L2 Service | LLM extraction matches operator-curated golden | ≥80% precision (services), ≥70% recall (file→service), 0 hallucinated dirs | L3 wire |
| L3 File summary | per-file LLM summary on 50 sampled files | 100% non-empty, ≤200 tokens each, 3–5 purpose_tags per file (vocabulary frozen during plan stage from a 50-file sample) | L4 unaffected (parallel) |
| L4 Symbols | tree-sitter on 200 Java/Py files | symbol count within ±5% of `ctags --recurse`; 0 parse errors on green files; CALLS edges > 0 per file with method calls | L5, L7 |
| L5 Chunks | top-1 recall on golden NL→file pairs | ≥85% top-3 recall on 30 hand-labelled pairs | L6 |
| L6 Translator | NL → grounded entities | ≥75% top-1 service correct, ≥60% top-3 file correct, 0 hallucinated names | L7 |
| L7 Bundle | golden ticket → bundle anchor coverage | ≥70% bundles contain ≥1 anchor file from gold | L8 |
| L8 E2E | full chat query → ContextBundle on real PosClientBackend | latency p50 ≤2.5 s, p95 ≤5 s; bundle non-empty; 0 exceptions over 50 queries | UnifiedContext rewire |

**README template per gate** (`README.md`):

```markdown
# Layer N — <name>

## Purpose
One sentence: what this layer guarantees.

## Fixture
- input file(s): paths
- size: N files, M LOC, K tokens
- repo snapshot: git sha or pack file

## Command
    pytest aiforge_core/codemem/tests/L<N>_<name>/ -v

## Pass criteria
- metric 1: threshold
- metric 2: threshold

## Sample expected output
(short JSON or text snippet)

## On failure
- check 1 (env, sidecar up, schema migrated)
- check 2 (model loaded, embed dim)
- escalation: open ticket CODEMEM-L<N>-<short>
```

**Test-data origin.** PosClientBackend is the primary fixture repo. Goldens (queries → expected anchors) are hand-curated once and reviewed quarterly. Schema regression is gated by `codemem.schema_version` plus a migration script for any node/edge label change.

## 8. Migration / cutover

New module lives beside the old code. No double-writes. Both alive until L8 green.

```
Step 0  scaffold codemem/ package + sqlite state db + tests/ tree
Step 1  Stage 1+2 ingest (Repo node + RUNBOOK)              gate L1
Step 2  Stage 3 (Service extract + services.yaml)           gate L2
Step 3  Stage 4+5 (treesitter + call edges)                 gate L4
        deprecate index/treesitter_ingest.py + index/graphify_loader.py
Step 4  Stage 6 (file summaries)                            gate L3
        deprecate index/repo_learn.py + index/repo_notes.py
Step 5  Stage 7 (chunk embeddings)                          gate L5
        deprecate index/symbol_embed.py
Step 6  Stage 8 (service deps; bonus, spot-check 5 known)
Step 7  Translator (Stage T) + fastpath                     gate L6
Step 8  Bundle (Stage R + B)                                gate L7
Step 9  Wire UnifiedContext.for_* → codemem.query           gate L8
        + A/B vs HEAD on 20 real tickets, ≥ parity
        old sources 1–3 deleted; sources 4–8 kept
Step 10 Delete legacy modules; learner_writeback wired
        rm aiforge_core/index/{treesitter_ingest, graphify_loader,
                               repo_learn, repo_notes, symbol_embed}.py
        rm aiforge_core/memory/code_context.py
        prune memory/unified_query.py to T1–T3 only
Step 11 Cross-repo edges (CALLS_REPO) — separate spec
```

**Rollback per step.**

- Steps 1–6: revert via `MATCH (n) WHERE n.schema_version = 'codemem-vN' DETACH DELETE n`. Old graph is untouched (different labels/properties during coexistence).
- Steps 7–9: feature flag `AIFORGE_CODEMEM_QUERY=1`. Flipped off, UnifiedContext routes to old sources.
- Steps 10–11: only after a 2-week soak.

**Schema namespacing during coexistence.** New labels are `Repo`, `Service`, `File_v2`, `Symbol_v2`, `Chunk_v2` (existing graphify uses `:Symbol`, `:Chunk`). After Step 10, drop `_v2` via single migration.

**State db (`~/.aiforge/codemem.state.db`).** Tables: `merkle_files`, `merkle_repo`, `service_overrides`, `query_cache`. Wiped via `aiforge codemem reset --repo X`.

## 9. Operator CLI

```
aiforge codemem ingest <repo>          # full ingest
aiforge codemem ingest <repo> --watch  # FS watcher, dev mode
aiforge codemem query "fix payment"    # debug retrieval
aiforge codemem stats <repo>           # node/edge counts, last index, drift
aiforge codemem reset <repo>           # nuke + reindex
aiforge codemem services <repo>        # show services.yaml + DEPENDS_ON
aiforge codemem doctor                 # check Neo4j, embed sidecar, RepoMix, llm
```

## 10. Error handling

| Where | Failure | Response |
|---|---|---|
| Stage 1 | `repomix` binary missing | `doctor` flags it; ingest aborts cleanly with install hint; never partial-writes |
| Stage 1 | repo > 200K tokens | auto-shard by top-level dir, summarize per-shard, merge |
| Stage 2/3 | LLM down / timeout | retry 3× with 5/15/45 s backoff; on full fail mark `Repo.last_indexed_at=null, last_error=...`; old bundle still served |
| Stage 2/3 | LLM returns invalid JSON | strict-JSON retry once; second fail logs to `bundle.errors`, leaves Repo/Service untouched |
| Stage 3 | LLM picks non-existent file path | drop silently, log; never invents nodes |
| Stage 4 | tree-sitter parser crash | quarantine file (`indexed_at=null, last_error='ts_crash'`), continue rest of repo |
| Stage 5 | call resolution ambiguous | record edge with `confidence < 1.0`; bundle uses confidence as tiebreak |
| Stage 6 | per-file summary times out | skip; previous summary retained; counter `summary_skipped++` |
| Stage 7 | embed sidecar :8764 down | retry 3×; persistent fail → ingest continues without vectors (chunk nodes stored, vec property null); query falls back to fulltext + graph only |
| Stage 8 | DEPENDS_ON returns 0 edges | acceptable; logs INFO; not a failure |
| Translator | LLM hallucinates entity not in catalog | drop hallucination, log to `bundle.errors`, fall back to top embed candidates |
| Translator | LLM returns empty | use embed top-K directly as anchors |
| Cypher | Neo4j connection failure | `codemem.query` returns empty bundle with `errors=['neo4j_down']`; UnifiedContext falls back to operator memory + repo doc only |
| Bundle | token budget overflow | drop in priority order (8 → 2); priority 1 is hard-required |
| Bundle | empty result | return ContextBundle with `errors=['no_anchors']`; agent prompt unchanged but flagged |

**Soft-fail invariant** (matches today's UnifiedContext): every source can fail; `bundle.errors` carries them; the caller never raises.

## 11. Observability

- `codemem.ingest.{repo}.{stage}.{start|end|error}` with `duration_ms`, `tokens_in/out`
- `codemem.query.{fastpath|translator|cypher|bundle}.{...}` same shape
- `codemem.cache.{hit|miss}`
- All emitted via existing `hooks.emit_step`; visible in `/api/runtime/perf?ticket=…` waterfall.

`aiforge codemem stats` example:

```
Repo:           1 (PosClientBackend)
Services:       7
Files:          834 (12 quarantined, 822 summarized)
Symbols:        14,219
CALLS edges:    11,402 (avg confidence 0.91)
Chunks:         3,118 (3,118 with vectors)
Last indexed:   2026-04-30 19:14 UTC (8m ago)
Drift since:    14 files changed, not yet re-ingested
```

## 12. Acceptance — design is "done" when

1. All eight L-gates green on the PosClientBackend fixture.
2. `codemem.query()` p50 ≤ 2.5 s, p95 ≤ 5 s on the 50-query smoke.
3. **Pain A test:** query "which services consume `business.push.request`" → returns `PosServerBackend.ClientSyncPushRequestConsumer` ranked top.
4. **Pain C test:** query "how do I run PosClientBackend tests" → returns `./mvnw test` plus the portforward block from the Repo node.
5. **Pain E test:** query "fix payment processing" → top file matches a manually-curated golden of 5 files; no rediscovery LLM call needed inside doer.
6. UnifiedContext still serves planner/doer/chat with no breaking change to the caller signature; sources 1–3 collapsed, sources 4–8 untouched.
7. Legacy modules (`index/treesitter_ingest`, `graphify_loader`, `repo_learn`, `repo_notes`, `symbol_embed`, `memory/code_context`) deleted.
8. `aiforge codemem doctor` reports green on Mac Studio + NUC.
9. Operator can override LLM-extracted services via `.aiforge/services.yaml`; re-ingest reflects the override.
10. Each test layer L1–L8 has a README beside it matching the §7 template.

## 13. Open questions (deferred)

- Cross-repo `CALLS_REPO` edges — separate spec after Step 10.
- Cold-start ingest for repos with >5,000 files — sharding strategy beyond Stage 1's auto-shard.
- Per-role bundle weighting (planner vs doer prefer different priorities) — current §6 priority order is uniform; revisit after L8 telemetry.
- Decay policy on `File.summary` learner appends — currently unbounded append; revisit when the first file summary exceeds budget.
