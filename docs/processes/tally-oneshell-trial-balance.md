# Tally ↔ OneShell Trial-Balance Reconciliation

**Purpose**: catch out-of-sync ledger balances between Tally (the
authoritative external accounting system) and OneShell POS for a
business across an environment (QA or PROD).

**Owner**: agent (`aiforge_core.aiforge_agents.processes.trial_balance`).
This document is also ingested into AiForgeMemory so the local LLM
sees it as part of every reconciliation ticket's `context_md`.

---

## Inputs

A ticket attaches **exactly two files**, names matching:

| filename pattern (case-insensitive) | source |
|---|---|
| `*tally*` (`.csv` / `.xlsx` / `.xls`) | export from Tally Trial Balance report |
| `*oneshell*` (`.csv` / `.xlsx`) | export from OneShell `chartOfAccounts` aggregation |

Optional ticket label: `env=qa` or `env=prod` (default `qa`).

## Expected file shape

### Tally export (canonical column names)

| column | type | notes |
|---|---|---|
| `Particulars` | str | account name, may be nested ("Sundry Debtors :: ACME Ltd") |
| `Group` | str | parent group (e.g. "Current Assets") |
| `Opening Balance` | decimal | OB at FY start; DR positive, CR negative |
| `Debit` | decimal | period DR |
| `Credit` | decimal | period CR |
| `Closing Balance` | decimal | DR positive, CR negative |
| `Account Code` | str | optional — Tally master account number |

### OneShell export (canonical column names)

| column | type | notes |
|---|---|---|
| `accountName` | str | leaf account name |
| `parentName` | str | one-level-up parent |
| `accountCode` | str | OneShell master `code` field |
| `openingBalance` | decimal | DR positive, CR negative |
| `periodDebit` | decimal | sum of period DRs |
| `periodCredit` | decimal | sum of period CRs |
| `closingBalance` | decimal | DR positive, CR negative |
| `businessId` | str | scoped business |

## Stage 1 — File validation (always runs first)

Before anything else, both files are passed through `validate_file()`:

| check | failure mode |
|---|---|
| file exists | `file not found: <path>` |
| not empty | `empty file` |
| readable as csv/xlsx/xls | `unreadable: <reason>` |
| has at least one data row | `no data rows` |
| required name column present (Particulars/Account/Name/Ledger for Tally; accountName/name/account/ledger for OneShell) | `missing any-of name column: …` |
| numeric column parses on first 50 rows | warning only |

CLI: `--strict` exits 2 on any validation error. Default = continue
with a printed warning.

## Stage 2 — Live OneShell from MongoDB (3-way mode)

When `--validate-with-mongo` is set (or the orchestrator detects a
business-id pattern in the ticket), the validator also pulls live
OneShell rows from `chartOfAccounts` keyed by `businessId`.
Connection priority:

1. `AIFORGE_MONGODB_SERVICE_URL` — HTTP gateway (production rule:
   never bypass MongoDbService).
2. `AIFORGE_MONGO_URI` — direct pymongo URI for QA convenience.
3. `mongodb://localhost:27017/oneshell` fallback for dev.

Best-effort: any failure (network, auth, schema) silently falls back
to plain 2-way file-vs-file recon. The CLI prints `WARN mongo fetch
errored: …` so the operator sees what happened.

## Reconciliation algorithm

```
1. Load both files as data frames (csv/xlsx auto-detected).
2. Normalise account names:
   - lowercase, trim, drop trailing punctuation
   - collapse "Sundry Debtors :: ACME Ltd" → ("Sundry Debtors", "ACME Ltd")
3. Join key: `accountCode` if present in BOTH, else normalised account name.
4. For each joined row, compute:
   - delta_open    = Tally OB        - OneShell OB
   - delta_dr      = Tally Debit     - OneShell periodDebit
   - delta_cr      = Tally Credit    - OneShell periodCredit
   - delta_close   = Tally CB        - OneShell CB
   - all deltas are absolute INR; round to 2dp.
5. Bucket each row into:
   - MATCH   when |delta_close| ≤ 1.00 (₹1 rounding tolerance)
   - DIFF    when 1.00 < |delta_close| ≤ 100.00 (review)
   - LARGE   when |delta_close| > 100.00         (block)
6. Tally-only / OneShell-only rows: list separately.
7. Compute totals:
   - total Tally CB sum, total OneShell CB sum, gap.
   - count by bucket.
```

## Sign convention (critical)

Both systems use **DR positive / CR negative** for closing balance.
Reconciliation arithmetic happens on the signed number — so
"Sundry Debtors" should be positive in both and a Tally value of
₹50000 means OneShell value of ₹50000 too. If one side is negative,
the gap is `2 × value`, not zero.

## Common gotchas

1. **Suspense accounts** — Tally has them, OneShell may not. Surface
   as a Tally-only row labelled `suspense_likely`.
2. **Opening balance lag** — first ticket of a fresh FY: OneShell may
   still have OB=0 until the YE close runs. Don't fail the ticket;
   raise as a soft warning.
3. **Inter-business transfers** — OneShell scopes by `businessId`;
   Tally rolls up. If totals diverge by an amount that matches another
   business's CB, link it as a probable transfer.
4. **Currency** — both are INR. If you ever see USD, fail loud.
5. **Empty rows** — Tally exports often have summary rows with bold
   account names and no numbers; drop rows where all balance fields
   are blank.

## 3-way mode adds two more recons

When DB rows are available, `reconcile_3way()` runs all three pairs:

| pair | catches |
|---|---|
| File ↔ DB | export drift — the file the operator uploaded doesn't match live DB |
| Tally ↔ File | classic recon, what the auditor sees |
| Tally ↔ DB | live truth check, ignores any export bug |

Each pair gets its own bucket counts + per-row CSV
(`<env>-<business>-{file-vs-db|tally-vs-file|tally-vs-db}.csv`).

## Outputs

The validator emits both:

| artefact | format |
|---|---|
| Console report | tabular, colour-coded by bucket |
| JSON summary | `{tally_total, oneshell_total, gap, buckets:{match,diff,large}, …}` |
| Markdown report | dropped into the ticket's MR body / comment |
| CSV per-row | `<env>-<businessId>-trial-balance-diff.csv` for download |

When `LARGE` rows exist OR `|gap| > 1000` the ticket auto-transitions
to `blocked` with a comment listing top 10 worst offenders.

## How agents use it

Any ticket whose `labels` include `trial-balance` and that has 2
attachments matching the patterns above gets routed through:

```
Understander → Tally/OneShell file detection (process flow above)
            → Trial-Balance Validator (deterministic Python)
            → Architect (writes summary into MR/comment)
            → Learner (records gap totals as recurring failures
              if same accounts diverge across runs)
```

The validator is **deterministic** — no LLM call. The LLM only
narrates the result. This keeps the recon reproducible and audit-
friendly for finance.
