# ONE-55 — Architect deep-dive: TallyConnector comprehension probe

**Written by**: Software Architect (Claude Code)
**Date**: 2026-04-20
**Ticket ID**: ONE-55
**Branch**: none (Architect-only — probe, not a code change)
**Assignee**: Architect (Claude Code) — measures memory-system coverage on one repo

## Architect-only rationale

This is a diagnostic, not feature work. Output is a self-report measuring how much an Architect agent can understand about `TallyConnector` using only the AIForgeCrew memory/RAG/skill surface (hindsight + ChromaDB RAG + aiforge-search skill + local file reads). No SrDev or Developer involvement. Result feeds back into the memory system design — where are the gaps, what needs seeding, what works.

## Probe method

1. Run `aiforge-search` with queries: "TallyConnector architecture", "TallyConnector entity sync order", "TallyConnector retry dead-letter", "TallyConnector authentication".
2. Read `TallyConnector/README.md` + `TALLY_SYNC_DATA_REFERENCE.md` directly.
3. List source tree depth: client/, config/, controller/, service/, sync/, tdl/, xml/, model/.
4. Cross-reference with inventory fact (ONE-54 seed) + RAG hits for consumer (`PosDataSyncService`).

## Comprehension report

### What I learned (high confidence)

**Purpose**: Java service (port 8085) acting as on-prem bridge between Tally ERP (port 9000 HTTP/ODBC) and OneShell cloud (`PosDataSyncService` → `MongoDbService` → MongoDB). Bidirectional sync — Tally-to-POS (pull + push) and POS-to-Tally (cloud-pull + TDL import).

**Tech stack**: Java 24 · Spring Boot 4 · JAXB (XML binding) · H2 file DB (persistent retry queue) · GraalVM native-image build option. Maven build; Dockerfiles for JVM + native modes.

**Sync flow (Tally → POS)**:
1. `TallyPullScheduler` (cron, default `0 */30 * * * *`) pulls XML from Tally.
2. SHA-256 hash of pulled data; skip if unchanged.
3. `ExternalServiceClient.pushAndGetResponse()` POSTs to cloud (`/api/v1/data/tally-ingest`) with JWT + `X-Business-Id` header.
4. Per-item ACK via `IngestResponse`; successful items update `SyncCheckpoint` table.
5. HTTP errors + connection failures queue to H2 `PendingSync` with status=PENDING.
6. `RetryScheduler` (every 30s) applies exponential backoff (initial 20s, cap 1h, default 100 max attempts).
7. After max retries → `FailedSyncFileWriter` writes to `./data/dead-letter/`.

**Sync flow (POS → Tally)**: `CloudPullScheduler` pulls from `PosDataSyncService` export endpoints, TDL-imports into Tally.

**Entity ordering (critical)**: `units,currencies,voucher-types,groups,ledgers,cost-centres,godowns,stock-items,vouchers` — default sync order is NOT arbitrary; downstream entities depend on upstream masters being present. `MissingDependencyCreator` handles forward-refs when items arrive out-of-order.

**Data mapping** (from `TALLY_SYNC_DATA_REFERENCE.md`):
- 10,000+ docs synced for one business (Meco Racing FY24-25 sample).
- Stock Item → businessProducts (5,562); Sundry Debtors/Creditors → Parties (380); Ledger (non-party) → chartOfAccounts; vouchers → sales/purchases/paymentIn/paymentOut/manualJournal/saleOrder based on type.
- Dual-save pattern: party ledgers land in BOTH Parties + chartOfAccounts.
- `tallyMasterSettings` collection holds voucher-type routing + per-ledger name mappings.
- Deterministic UUIDs for idempotency.
- `tallyRawVouchers` = unmapped fallback (0 in reference sample — all mapped).

**Auth**: JWT-based. `CloudAuthTokenManager` hits `/auth/user/token`, refreshes `refresh-before-expiry-sec=300` ahead of expiry.

**Code structure**: 98 Java files across 8 packages:
- `client/` — `TallyHttpClient` + `TallyException`
- `controller/` — 13 REST endpoints (Export/Import/RawXml/Setup/SyncStatus/TrialBalance/Version/XmlFileUpload/XmlPreview/Home/HealthCheck/AutoUpdate + GlobalExceptionHandler)
- `service/` — `TallyService`, `ImportRequestBuilder`, `ImportXmlBuilder`
- `sync/` — schedulers + `SyncConfig`, `SyncLock`, `TallyErrorParser`, `FailedSyncFileWriter`, `MissingDependencyCreator` + entity/model/repository sub-packages
- `tdl/`, `xml/`, `config/`, `model/` — schema + binding glue

**Deploy target**: Installer (Launch4J + NSIS) for on-prem Windows; JVM + native-image Docker for cloud.

### What I learned (medium confidence)

- `SyncLock` probably prevents concurrent pull/push races — not inspected in detail.
- `AutoUpdateController` suggests in-place updater for on-prem installs (companion `updater/` dir exists at root).
- `TrialBalanceValidationController` suggests post-sync reconciliation to catch dropped transactions.

### What I did NOT learn (gaps)

- Exact Tally TDL query syntax used (`tdl/` dir not read).
- H2 DB schema details beyond the three entities (`ActionRequiredItem`, `PendingSync`, `SyncCheckpoint`).
- Current production version + deploy channels (staging vs. customer-installed).
- Known bugs / tech debt / recent incidents — **nothing in hindsight beyond the one-line inventory fact**. No `claude-md` memory (my `~/.claude/memory` notes on Tally live on the Mac Studio, not laptop).
- Relationship to `TallyConnector` commits that touched ONE-tickets previously — git log not queried.

## Memory-system self-assessment

**What worked well**:
- `aiforge-search` returned inventory fact + RAG hits to `PosDataSyncService`'s `tally/` consumer classes on the first query — zero exploration turns needed.
- README + TALLY_SYNC_DATA_REFERENCE are authoritative; reading them produced 90% of the above.
- RAG index covers this repo (`tally:README.md`, `pds:*` for consumer).

**Gaps the probe exposed**:
1. **No incident/retro memory**: hindsight has inventory + pipeline canon but no Tally-specific incident facts. If an agent hits a known Tally bug, it can't recall prior diagnosis.
2. **Mac Studio claude-md not reachable from laptop**: my `~/.claude/memory/tally-*.md` files (tally-architecture.md, tally-testing-sop.md, tally-versions-and-fixes.md) live on the Mac Studio. `aiforge-search` Source 3 (claude-md grep) runs on the Mac Studio so it would find them, but this report was drafted on laptop — local grep returned 0.
3. **No `tdl/` xml-template indexing**: the *.tdl files in TallyConnector are likely not markdown/java → may not be in RAG chunk. Worth checking.
4. **No per-repo "last known version" fact**: hindsight has a world fact listing MongoDbService/PosClientBackend/PosFrontend versions but not TallyConnector (1.1.4 from README — should be seeded).

**Recommendations for follow-up tickets**:
- ONE-56: seed Tally-specific experience facts from `~/.claude/memory/tally-*.md` (on Mac Studio) into hindsight aiforge bank, same direct-SQL pattern as ONE-54.
- ONE-57: add TallyConnector's `tdl/` directory to `scripts/rag-reindex-multi.py` if not already indexed.
- ONE-58: periodic auto-discover repo versions from `pom.xml` / `package.json` + upsert as hindsight facts.

## Acceptance criteria (for this probe)

- [x] Probe executed against TallyConnector using only AIForgeCrew memory/search surface.
- [x] Comprehension report captures high/medium/gap buckets.
- [x] Gaps enumerated with concrete follow-up ticket ideas.
- [x] Report committed to `docs/tickets/ONE-55.md` for future reference.

## Constraints / non-goals

- DO NOT fix any TallyConnector code from this ticket.
- DO NOT dispatch to Sr Dev / Developer — this is diagnostic only.
- OUT OF SCOPE: actual memory remediation (that's ONE-56/57/58).

---

**Sr Developer**: SKIP.
**Developer**: SKIP.
