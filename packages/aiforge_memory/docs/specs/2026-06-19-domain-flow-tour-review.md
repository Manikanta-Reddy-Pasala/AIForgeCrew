# Domain/Flow · Tours · Graph-Review — Design

**Date:** 2026-06-19
**Status:** Approved → implementing
**Origin:** Capabilities learned from Egonex-AI/Understand-Anything
(domain-analyzer, tour-builder, graph-reviewer) ported into AFM's
`features/` pattern. Reuses the existing `:Repo/:Service/:File_v2/:Symbol_v2`
graph + `CALLS/IMPORTS/CONTAINS_FILE/OWNS_SERVICE` edges — **no new ingest**.

## Principle

**Deterministic-first.** Each feature computes its structure from graph
topology (testable without a live LM); an **optional** LLM pass only
names/narrates. LLM is monkeypatchable + env-gated
(`AIFORGE_CODEMEM_LM_URL`/`_MODEL`, same as `service/extract`); when the
LM is unreachable or `AIFORGE_CODEMEM_NAMING=0`, deterministic labels are
used. This keeps unit tests LM-free and the features usable offline.

## Feature 1 — `features/domain`

Semantic domains + ordered flows over the service/symbol graph.

- **extract.py** `extract_domains(driver, *, repo) -> DomainResult`
  - Deterministic: cluster `:Service` nodes into domains by CALLS_REPO /
    shared-symbol connectivity (connected components over service-level
    call edges; singletons = own domain). Flows = ordered CALLS chains
    from entry symbols (low CALLS in-degree, api/main-tagged) via BFS,
    capped depth.
  - Optional LLM: name + one-line description per domain/flow from member
    symbol labels. Falls back to deterministic name (top service /
    entry-symbol) on LM failure.
  - Dataclasses: `DomainDraft{name, description, services[], key_symbols[]}`,
    `FlowDraft{name, description, steps:[{node_id, kind, label, order}]}`.
- **store.py** `upsert_domains(driver, *, repo, domains, flows) -> counts`
  - `MERGE (:Domain {repo,name})`, `(:Domain)-[:COVERS]->(:Service)`,
    `(:Domain)-[:KEY_SYMBOL]->(:Symbol_v2)`.
  - `MERGE (:Flow {repo,name})`, `(:Flow)-[:STEP {order}]->(:Symbol_v2|:File_v2)`.
  - Idempotent MERGE + prune stale Domains/Flows for the repo.
- **CLI** `aiforge-memory domains <repo> [--json] [--no-naming]`.

## Feature 2 — `features/tour`

Ordered onboarding walkthrough.

- **extract.py** `build_tour(driver, *, repo, domain=None) -> TourDraft`
  - Deterministic order: entry points (api/main/service-tagged, low
    in-degree) → BFS over CALLS → dedupe → ordered `stops`. Optional
    `domain` filter (restrict to a `:Domain`'s symbols).
  - Optional LLM: per-stop one-line "why you're here" narration; falls
    back to symbol `doc_first_line`/`summary`.
  - Dataclass `TourDraft{repo, domain, stops:[{node_id, kind, label,
    order, note}]}`; renders `tour.md`.
- **store.py** `upsert_tour(driver, *, repo, tour) -> counts`
  - `MERGE (:Tour {repo,name})`, `(:Tour)-[:STOP {order,note}]->(node)`.
  - Writes markdown artifact path returned to caller.
- **CLI** `aiforge-memory tour <repo> [--domain X] [--out tour.md] [--no-naming]`.

## Feature 3 — `features/review`

Graph completeness reviewer — distinct from `eval` (which measures
retrieval recall). Pure deterministic coverage scan + optional LLM
commentary.

- **extract.py** `review_graph(driver, *, repo) -> ReviewReport`
  - Checks: files with no symbols; symbols with no CALLS & no IMPORTS
    (orphans); services with no files; files missing summaries; `:Domain`
    coverage (services in no domain); dangling edges. Each → a
    `Finding{kind, severity, count, sample:[ids]}`.
  - Optional LLM: short "what's likely missing" note from the finding set.
  - Dataclass `ReviewReport{repo, findings:[Finding], totals:{...}}`.
- **store.py** (optional) `upsert_review(driver, *, repo, report)` →
  `:ReviewFinding` nodes for trend tracking; always returns JSON + md.
- **CLI** `aiforge-memory review <repo> [--json] [--store]`.

## Schema (`core/neo4j.py`)

Add constraints/indexes:
- `:Domain` UNIQUE (repo, name); `:Flow` UNIQUE (repo, name);
  `:Tour` UNIQUE (repo, name); `:ReviewFinding` index (repo, kind).
All `IF NOT EXISTS` (idempotent, matches existing style).

## Wiring (`query/bundle.py`)

Add recall sources so agents get domain/flow context:
- `domains` — `:Domain` for the repo (names + descriptions) →
  `bundle.sources_used.append("domains")`.
- `flows` — `:Flow` steps matching the query symbols →
  `sources_used.append("flows")`.
Tours are an onboarding artifact (CLI/file), not a per-query recall
source. Review is ops-only (not in bundle).

## Testing

Per feature, two tiers (mirrors `service/tests`):
- **Pure unit** (no Neo4j, no LM): deterministic extraction over a
  fake in-memory graph fixture; LLM monkeypatched; `_parse`/dataclass +
  store Cypher-string assembly via a fake driver recording `.run` calls.
- **`live_neo4j`** integration: ingest a fixture repo, run extract→store,
  assert node/edge counts + idempotency (re-run = no dup, prune works).
Plus a CLI smoke per command (argparse `register`/`run`).

## Out of scope
- New ingest/parsing (reuses existing graph).
- LLM-mandatory paths (LLM always optional).
- UI tabs (CLI + bundle only).
