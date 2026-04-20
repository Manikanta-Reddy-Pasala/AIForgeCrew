# ONE-48 — BOI Parser v2 Re-Evaluation (qwen-coder-next + gemma-4-31b-it)

**Created**: 2026-04-20
**Related**: ONE-46 (Bank of India OCR parser, branch `aiforge/qwen-boi`, commit `c52004b`)
**Sample**: `BOI.pdf` (PARI COMPUTERS PVT LTD, Bremen Chowk, Cash Credit, 12 txns, all deposits)

---

## Actual state of ONE-46 output (verified 2026-04-20 via code inspection)

Memory S1710 was partially incorrect. Real inspection of `app/util/boi_bank_handler.py` (103 lines, commit c52004b) on remote branch `aiforge/qwen-boi`:

### What qwen-coder-next did right (memory was wrong about these)

| # | Claim in memory S1710 | Actual code | Status |
|---|----------------------|-------------|--------|
| R1 | `account_number` = None | regex `Account No\s*:\s*(\d+)` extracts `052230110000005` | ✅ works |
| R2 | `account_name` = None | regex `Name\s*:\s*(.+?)\s+Account No\s*:` extracts `PARI COMPUTERS PVT LTD` | ✅ works |
| R3 | Direction hardcoded "CR" | `cr_dr = "CR" if deposits_amount else ("DR" if withdrawal_amount else "")` — inferred from columns | ✅ works |
| R4 | All-CR output = bug | Sample has **only deposits** → all-CR is correct. Memory misread | ✅ correct-but-untested |

### What qwen-coder-next actually missed

Real gaps (from code inspection, not memory):

| # | Gap | Severity | Evidence |
|---|-----|----------|----------|
| G1 | Schema wrong: `crDr` vs `transactionDirection`, `accountName` vs `account_name`, `date` vs `txn_date`, no `metadata` wrapper | **Blocker** for downstream consumers | handler lines 46-52 |
| G2 | `chequeNo` returns `""` when blank, not `null` | High (type inconsistency) | `clean_text(row[3]) if ... else ""` |
| G3 | Date kept as raw `"02-04-2026"` (DD-MM-YYYY) not ISO `"2026-04-02"` | High | `row[1].strip()` |
| G4 | Amounts as Python `float` — precision loss risk | Medium (money) | `parse_numeric` returns `float()` |
| G5 | No metadata fields: `bank_name`, `branch`, `ifsc_code`, `customer_id`, `account_type`, `statement_period_start/end`, `statement_generated_at` | **Blocker** | no extraction attempt |
| G6 | `transactionId` field holds serial row number, not an actual txn reference | Medium (misleading field) | `row[0].strip()` |
| G7 | `account_number` regex brittle if header layout wraps | Low | regex requires `\s+Account No` on same extracted line |
| G8 | PDF path in tests hardcoded to `app/routes/BOI.pdf` not `tests/fixtures/` | Low | test files |

### What tests actually missed

`tests/util/test_boi_bank_handler.py` (64 lines, 4 tests):

| Existing test | What it checks | What it misses |
|---------------|----------------|----------------|
| `test_boi_handler_extracts_account_details` | accountName + accountNumber string equality | metadata completeness (G5) |
| `test_boi_handler_extracts_transactions` | first txn has keys, IDs "1" & "12" present | field values |
| `test_boi_handler_has_non_empty_transactions` | at least one non-empty txn | weak — any output passes |
| `test_boi_handler_correct_transaction_count` | count == 12 | amounts/dates/balances/directions all unchecked |

**Not tested**: direction per row, amount values, balance values, description content, chequeNo type, date format, ledger sanity.

A handler that returns 12 dicts with correct `transactionId` = "1"..."12" but wrong everything else passes all 4 tests. **This is the real quality gap.**

---

## ONE-48 — Acceptance criteria (v2 handler)

Both candidate models (qwen-coder-next, gemma-4-31b-it) must produce:

1. `PosPythonBackend/app/util/boi_bank_handler.py` exposing `parse(pdf_path: str) -> dict`
2. Return schema matches `docs/tickets/fixtures/boi_expected.json` — `metadata` wrapper + `transactions` list with fields: `serial, txn_date (ISO), description, chequeNo (nullable), transactionAmount, transactionDirection (CR|DR), balance`
3. `PosPythonBackend/tests/util/test_boi_bank_handler.py` = contents of `docs/tickets/ONE-48-test-spec.py` (T1–T11 + T2b, T3b)
4. `PosPythonBackend/tests/fixtures/BOI.pdf` — copied from `app/routes/BOI.pdf`
5. `PosPythonBackend/tests/fixtures/boi_expected.json` — copied from `docs/tickets/fixtures/boi_expected.json`
6. All 12 tests pass via `./venv/bin/python -m pytest tests/util/test_boi_bank_handler.py -v`
7. Branch: `aiforge/ONE-48-<model>-boi-v2`
8. Commit SHA + PR URL in ticket comment before marking done

---

## Re-evaluation matrix

| Run | Model | Branch | Metrics recorded |
|-----|-------|--------|------------------|
| A | `qwen3-coder-next` (80B MoE, 4-bit MLX) | `aiforge/ONE-48-qwen-boi-v2` | tests_passed/12, wall_seconds, input_tokens, output_tokens, commit SHA, PR URL, hallucination_flag |
| B | `gemma-4-31b-it` (31B dense, 4-bit MLX) | `aiforge/ONE-48-gemma-boi-v2` | same |

Runs sequential (LM Studio loads one model at a time). Gemma caveat: strict chat template re-downloaded 2026-04-20 — may trigger compression errors per memory 6411.

Output logged to `docs/eval/boi-v2-bench.csv`.

---

## Status

- [x] Ticket written 2026-04-20
- [x] Golden fixture `docs/tickets/fixtures/boi_expected.json` (12 txns hand-verified against PDF)
- [x] Test spec `docs/tickets/ONE-48-test-spec.py` (T1–T11, T2b, T3b)
- [ ] Push fixtures to remote PosPythonBackend
- [ ] Dispatch Run A (qwen-coder-next)
- [ ] Dispatch Run B (gemma-4-31b-it)
- [ ] Compare + log to `docs/eval/boi-v2-bench.csv`
- [ ] Update hindsight memory with corrected S1710 findings
