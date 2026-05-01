# Process — Trial-Balance Verification (Tally ↔ OneShell)

This document is **the playbook**. When a ticket arrives with the
label `trial-balance` and 2 attachments matching `*tally*` / `*oneshell*`,
the agents read this doc out of memory and follow the steps below
**instead of generating Java code**. This is a workflow process, not
a code-change ticket.

---

## When this process applies

Trigger conditions (any combination):

| signal | example |
|---|---|
| ticket label | `trial-balance` |
| ticket title / body keywords | "trial balance", "validate balance", "tally vs oneshell" |
| 2 attachments | one filename matches `*tally*`, one matches `*oneshell*` (csv / xlsx / xls) |

When all of those are missing, agents should fall back to the normal
code-change pipeline.

---

## Step-by-step (what each agent does)

### 1. Understander
- Pull this playbook out of AiForgeMemory (it's indexed as Chunk_v2
  under `docs/processes/trial-balance-flow.md`).
- Surface the playbook into `understanding.context_md` so every
  downstream agent sees it.
- Identify the business id (regex `\bb\d{14,}\b` against title+body).
- Identify the environment (qa / prod) — default qa.

### 2. Planner
- Do NOT generate file-creation steps.
- Emit a 3-step plan:
  1. `run` — `aiforge-agents-tb --tally <attachment-tally> --oneshell <attachment-oneshell> --env <env> --business <bid> [--validate-with-mongo]`
  2. `read` — the resulting markdown report (`/tmp/<env>-<bid>-tb-report.md`)
  3. `run` — post the report into the ticket comments

### 3. Validator (deterministic process)
The orchestrator's `_maybe_run_trial_balance` short-circuit runs the
deterministic Python validator. The validator does:

  a. **Validate files first**
     - file exists, non-empty, readable as csv/xlsx/xls
     - required name column present (Particulars / Account / Name / Ledger)
     - numeric column parses on first 50 rows
     - strict mode halts on any error

  b. **Compute totals (top-down)**
     - For each source (Tally, OneShell file, OneShell DB / API),
       sum OB, DR, CR, CB across all rows.
     - Compare each pair of sources at the totals level.
     - If every dimension is within ₹1 → **DONE, all good. Skip drill.**
     - If any dimension diverges → **drill down**.

  c. **Drill down per-account** (only on mismatch)
     - For every unique account (key = code OR normalised name) across
       all sources, compute deltas DR/CR/OB/CB.
     - Tag each account with one or more diagnoses:
       * `tally_only` — present in Tally, missing in OneShell
       * `oneshell_only` — present in OneShell, missing in Tally
       * `opening_balance_mismatch` — OB diff > ₹100
       * `debit_only_drift` — DR diverges, CR matches → missing DRs
       * `credit_only_drift` — CR diverges, DR matches → missing CRs
       * `both_sides_drift` — DR and CR both off
       * `db_vs_api_drift` — chartOfAccounts CB ≠ /trialBalance API CB
       * `file_vs_api_drift` — uploaded export stale vs live API

  d. **Fetch live OneShell data** (3-way mode)
     - Priority: PCB `GET /v1/api/chartOfAccounts/trialBalance?businessId=...&financialYear=...` (authoritative)
     - Fallback: direct `chartOfAccounts` collection in Mongo (opening only)
     - Fallback: file-only 2-way

  e. **Emit report**
     - JSON summary (totals, gaps, bucket counts)
     - Markdown report (3 tables: totals / pairwise compares / per-account drill)
     - Per-row CSVs at `<env>-<bid>-{file-vs-db|tally-vs-file|tally-vs-db}.csv`

### 4. Architect
- Read the markdown report.
- Decide:
  * `approve` — all totals matched OR only `match` bucket non-empty
  * `request_changes` — any LARGE bucket OR top-line mismatch
- Write a 1-paragraph summary into `review.mr_body` calling out the
  top-3 worst-offending accounts.

### 5. Learner
- Record per-account diagnoses with `seen_count` so accounts that
  diverge across multiple recons bubble to the top of the playbook
  in future runs.
- `task_class = "trial-balance"` so skill / failure recall is shared
  across all such tickets.

---

## Sign convention

Both Tally and OneShell **should** use DR-positive / CR-negative for
closing balance. Real-world OneShell exports sometimes invert all
signs (an export-script bug). The validator does NOT auto-correct;
it surfaces the divergence as `opening_balance_mismatch` on every
asset row and leaves the call to finance.

If the report shows EVERY non-zero account as `Tally ₹X vs OneShell
₹-X`, the OneShell export has a sign-flip bug. Fix the export, do
not fix the validator.

---

## File schema reference

### Tally (xlsx)
Title rows above header are tolerated (auto-detected). Real Tally
exports have 3-row header:
```
Row N:   Particulars | <date range>
Row N+1:             | Opening                       | Transactions          | Closing
Row N+2:             | Balance | Debit | Credit | Balance
```
Merged column names: `Particulars`, `Opening Balance`, `Transactions
Debit`, `Credit`, `Closing Balance`.

### OneShell (xlsx) — native schema
| col | type |
|---|---|
| `accountName` | str |
| `accountCode` | str |
| `parentName` | str |
| `openingBalance` | decimal |
| `periodDebit` | decimal |
| `periodCredit` | decimal |
| `closingBalance` | decimal |
| `businessId` | str |

### OneShell (xlsx) — Tally-shaped variant
Some exports mirror the Tally column layout. The validator
auto-detects via column names + falls back to lenient parsing.

---

## CLI quick-reference

```bash
# Plain 2-way (no live DB)
aiforge-agents-tb \
    --tally    ~/Downloads/tally.xlsx \
    --oneshell ~/Downloads/oneshell.xlsx \
    --env qa --business b117754083966041 \
    --out-dir /tmp/tb-out

# 3-way (Tally + File + Live OneShell from PCB API)
AIFORGE_PCB_API_BASE=http://localhost:8090/v1/api \
AIFORGE_MONGO_URI=mongodb://databaseAdmin:***@localhost:27017/oneshell?authSource=admin \
aiforge-agents-tb --tally A.xlsx --oneshell B.xlsx \
    --env qa --business b117754083966041 \
    --validate-with-mongo

# Strict mode — exit 2 on any file-validation error
aiforge-agents-tb --tally bad.xlsx --oneshell B.xlsx --strict
```

Exit codes:
- `0` — all match (≤ ₹1 per dimension)
- `1` — top-line mismatch / LARGE bucket non-empty
- `2` — file validation failed (strict mode)

---

## Memory ingestion

Ingest this file into AiForgeMemory so agents pick it up automatically:

```bash
aiforge-memory ingest AIForgeCrew --path /Users/manip/Documents/codeRepo/AIForgeCrew --force
```

After re-ingest, `Chunk_v2` rows for `docs/processes/trial-balance-flow.md`
become vector-searchable. Any future ticket containing `trial balance`,
`tally`, `oneshell`, or `reconcile` will surface this playbook in
`context_md` automatically — no code change required.

---

## Diagnoses cheat-sheet

| diagnosis | what to fix |
|---|---|
| `tally_only` | OneShell missing master record — create `chartOfAccounts` row OR re-route business id |
| `oneshell_only` | Tally export missed it — re-export with all groups expanded |
| `opening_balance_mismatch` | Run OneShell year-end close to repopulate OB; or re-import OB from Tally |
| `debit_only_drift` | Missing DR transactions on OneShell — find sale/payment receipts not yet synced |
| `credit_only_drift` | Missing CR transactions — usually expense/payment-out entries lagging |
| `both_sides_drift` | Whole period of transactions out — re-trigger NATS sync from PosClientBackend |
| `db_vs_api_drift` | `trialBalanceCacheService` cache stale — invalidate the per-business key |
| `file_vs_api_drift` | The uploaded export was generated against an older snapshot — regenerate |

---

This playbook is the source of truth. Update it when the recon flow
changes; agents will see the new version on next memory ingest.
