# Phase 2 — Embedded SQLite Memory Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use `- [ ]`.

**Goal:** Let agents write + recall memory with zero external infra (no Neo4j, no Postgres, no embed sidecar), via a SQLite store with an offline hash-embedding and brute-force cosine recall. Pro backends (Neo4j / Postgres / AFM bundle) stay unchanged and take over automatically when their env vars are set.

**Architecture:** A `backend_select.memory_backend()` returns `sqlite` by default, `neo4j`/`postgres` when their env present. `local_embed.embed()` is a deterministic dependency-free hash embedding. `sqlite_memory` stores units + embeddings in `~/.aiforge/memory.db` and recalls by cosine. `unified_query.query()` gains a SQLite recall source (soft-fail, only active when embedded). The three write paths (`learner_persist`, `failure_memory`, `tools/memory_write`) route to `sqlite_memory` when embedded.

**Tech Stack:** Python stdlib `sqlite3`, `hashlib`/`math`, pytest. No new third-party deps.

Spec: `docs/superpowers/specs/2026-06-20-deploy-anywhere-design.md` §4.2. Embedded recall is degraded (lexical-vector, no graph-hop/domains/tours) — that is intended.

---

## File Structure

- Create `aiforge_core/memory/local_embed.py` — offline hash embedding (`EMBED_DIM=256`, `embed(text)->list[float]`).
- Create `aiforge_core/memory/sqlite_memory.py` — `write_unit`, `recall`, `stats`, schema, dedupe.
- Create `aiforge_core/memory/backend_select.py` — `memory_backend()`, `embedded()`.
- Modify `aiforge_core/memory/unified_query.py` — add SQLite recall source (slot #1).
- Modify `aiforge_core/runtime/learner_persist.py` — embedded write branch.
- Modify `aiforge_core/runtime/failure_memory.py` — embedded write branch.
- Modify `aiforge_core/runtime/tools/memory_write.py` — embedded write branch.
- Tests: `tests/python/test_local_embed.py`, `test_sqlite_memory.py`, `test_backend_select.py`, `test_unified_query_sqlite.py`, `test_memory_write_routing.py`.

## Tasks

1. `backend_select.py` + env tests — selector returns sqlite unless `AIFORGE_MEMORY_BACKEND`/`NEO4J_*`/`AIFORGE_PG_URL` present.
2. `local_embed.py` + tests — deterministic, dim 256, L2-normed, lexical similarity ordering.
3. `sqlite_memory.py` + tests — write/recall cosine ranking, repo filter, (repo,text) dedupe, stats.
4. `unified_query` SQLite source + test — embedded mode surfaces sqlite hits, tagged `source="memory"`, soft-fail.
5. Write-path routing (`learner_persist`, `failure_memory`, `memory_write`) + tests — embedded mode writes to sqlite, returns same result dict shape, never raises.

Selector rules (Task 1):
- `memory_backend()` → `"neo4j"` if `AIFORGE_MEMORY_BACKEND=="neo4j"` OR (`AIFORGE_NEO4J_URI` or `NEO4J_URI` present in env). `"postgres"` if `AIFORGE_MEMORY_BACKEND=="postgres"` OR `AIFORGE_PG_URL` present. Else `"sqlite"`. `AIFORGE_MEMORY_BACKEND` explicit value wins.
- `embedded()` → `memory_backend()=="sqlite"`.

Unit row shape (Task 3): `id, kind, wing, source, title, text, tags(json), metadata(json), repo, ticket, embedding(json), event_time, created_at`. `recall()` returns hits `{text,title,source,kind,score,ticket,repo}` with `score` = clamped cosine in [0,1].
