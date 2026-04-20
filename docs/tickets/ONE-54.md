# ONE-54 — Build codeRepo inventory + seed hindsight canon

**Written by**: Software Architect (Claude Code)
**Date**: 2026-04-20
**Ticket ID**: ONE-54
**Branch**: none (Architect-only — no Sr Dev / Developer dispatch)
**Assignee**: Architect (Claude Code) — executes end-to-end; does not hand off

## Architect-only rationale

This ticket is metadata/documentation. No MongoDbService, PosClientBackend, or similar code touched. No tests to author. Sr Dev breakdown + Developer implementation would add latency with no value — a single Architect pass writes the inventory, seeds hindsight, and closes. **Do not dispatch via `scripts/ticket-run.sh`.**

## Involved repos

- `AIForgeCrew` — writes `docs/repo-inventory.md` + SQL seed script

## Problem

Hermes agents (Sr Dev, Developer) hit unfamiliar OneShell repos during ticket runs. Example: ONE-53 touched MongoDbService but needed context on PosClientBackend + oneshell-commons + Scheduler too. Agent wasted turns running `ls ~/codeRepo` + reading random files to orient.

There are 48 entries under `~/Documents/codeRepo/` (mirrored on Mac Studio at `~/codeRepo/`). Zero structured inventory. No single document tells an agent "PosDockerSyncService is legacy Docker-era sync, now superseded by NATS push." Agents guess, waste turns, or miss critical constraints.

## Why this matters

Every new ticket re-derives the same repo taxonomy from scratch. With 48 repos × ~5 min of orientation per ticket × N tickets/week, the cost compounds. Architect writes it once; all future agents recall from hindsight or RAG in one call.

Secondary: catches stale repos (archived, abandoned) — Architect marks them `status: archived` so agents skip them entirely.

## Design choice

Three outputs, single commit:

1. **`docs/repo-inventory.md`** — human-readable table. Columns: `repo | category | language | status | purpose | entry points | owned-by-v4-agents?`. Categories: core-service, support-service, shared-lib, infra, frontend, poc, archive, external. Status: active, maintenance-only, archived.

2. **`scripts/seed-repo-inventory.sh`** — generates 48 direct SQL inserts into hindsight `memory_units` (bank_id=aiforge, fact_type=world). One fact per repo. Runs idempotently (ON CONFLICT DO UPDATE on text). No LLM extraction needed — bypasses the NIM/LM Studio response_format blocker that hit us in the seed pipeline.

3. **`aiforge-search`** skill already queries hindsight — inventory becomes recallable instantly after seed.

**Rationale**: direct SQL insert sidesteps the hindsight LLM-extraction stall. Markdown inventory covers human readers; hindsight seed covers agent recall. RAG (ChromaDB) is not used for this — `repo-inventory.md` is small (~4KB) and the structured form is more useful via semantic recall than chunked RAG search.

**Alternatives rejected**:
- Let agents grep `find ~/codeRepo -maxdepth 2 -name README.md` mid-ticket — doesn't capture status/category/deprecation. Repeats work per ticket.
- Auto-generate from git log — misses human knowledge ("this was a POC we never killed").
- Commit READMEs to each repo — 48 cross-repo PRs, out of scope. This ticket lives inside AIForgeCrew only.

## Acceptance criteria

- [ ] `docs/repo-inventory.md` exists with ≥40 rows (48 dirs minus filter for non-repo entries like CLAUDE.md, memory/, docs/, skills/).
- [ ] Each row has: repo name, category (8 enum values), language, status (active/maintenance/archived), one-line purpose.
- [ ] `scripts/seed-repo-inventory.sh` runs without NIM or LM Studio — pure psql inserts.
- [ ] After seed, `aiforge-search` query `"TallyConnector purpose"` returns a hindsight hit (proves recall works).
- [ ] After seed, `SELECT COUNT(*) FROM memory_units WHERE bank_id='aiforge' AND tags && ARRAY['repo-inventory']` returns ≥40.
- [ ] Commit msg references ONE-54.

## Files likely touched

- `docs/repo-inventory.md` — new file (~100 lines)
- `scripts/seed-repo-inventory.sh` — new file (idempotent SQL)
- `docs/runbook.md` — add 2-line entry under "Rebuild RAG after doc edits": also run `bash scripts/seed-repo-inventory.sh` after inventory edits

## Reference patterns (prior art)

- `/tmp/seed-aiforge-facts.sql` (from session 2026-04-20) — precedent for direct SQL inserts into memory_units; 14 facts seeded that way survived the NIM/LM Studio failures
- `scripts/hermes-seed-memory.sh` — the LLM-extraction path we're bypassing, for why we picked direct SQL
- `docs/architecture.md` — Component list; inventory should complement, not duplicate

## Constraints / non-goals

- DO NOT commit READMEs to the 48 external repos. This ticket writes one doc inside AIForgeCrew only.
- DO NOT run the existing `hermes-seed-memory.sh` — it stalls on LLM extraction; use direct SQL.
- DO NOT include `CLAUDE.md`, `docs/`, `skills/`, `memory/` entries — these are AIForgeCrew repo-internal files, not sibling repos.
- OUT OF SCOPE: per-repo tech-debt analysis, code coverage metrics, dep graphs. Just inventory + purpose + status.

## Test strategy (self-check for Architect)

After commit:
1. `wc -l docs/repo-inventory.md` → ≥50 lines.
2. `bash scripts/seed-repo-inventory.sh` → exits 0, reports N inserts.
3. `PGPASSWORD=hindsight psql … -c "SELECT COUNT(*) … WHERE tags && ARRAY['repo-inventory']"` → ≥40.
4. Run `aiforge-search` smoke with query `"PosDataSyncService Tally"` — expect hit from the seeded inventory fact + any RAG matches.
5. If seed re-runs, counts stay stable (idempotency).

---

**Sr Developer**: SKIP. This ticket is Architect-only by design.
**Developer**: SKIP. This ticket is Architect-only by design.
