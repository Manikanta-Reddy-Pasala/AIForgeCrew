# ONE-53 — Stock transfer non-atomic warehouse+qty multi-step update

**Written by**: Software Architect (Claude Code)
**Date**: 2026-04-20
**Ticket ID**: ONE-53
**Branch**: `aiforge/ONE-53` (created by Architect in MongoDbService)

## Involved repos

- `MongoDbService` — only repo touched. Stock-transfer logic lives here.

## Problem

`ProductServiceImpl.doUpdateStockTransferStockAtomic` (around line 2285-2390 in `src/main/java/com/oneshell/mongodb/feature/product/ProductServiceImpl.java`) performs TWO separate MongoDB operations when a stock transfer applies to an existing warehouse element:

1. `collection.updateOne(query, update, options)` — updates `warehouseDetails.$[wh].qty` via `$inc` + arrayFilters. Also pulls `serialData` on decrement.
2. `mongoTemplate.updateFirst(query, qtyUpdate, ...)` — separately updates scalar fields `maxQuantity`, `availableQuantity`, `updatedAt`.

If step 2 fails (network blip, MongoDB flap, timeout) after step 1 succeeds, the warehouse array is mutated but the scalar product-level quantity totals are stale. Stock becomes internally inconsistent. Downstream code (ledger checks, low-stock alerts, sync to PosServerBackend) reads inconsistent state.

This is a confirmed bug. Validated during ONE-48 / ONE-50 investigation (see `docs/eval/stock-transfer-reasoning.md` and `docs/eval/stock-transfer-coder.md`).

## Why this matters

- **Data consistency blocker** for multi-tenant stock accuracy. Any partial-failure scenario writes corrupted state.
- Silent: no alert, no rollback. Only surfaces during audits or when a sync downstream catches the discrepancy.
- Stock-transfer is a high-frequency path (every order, every adjustment).

## Design choice

**Merge scalar qty increments into the same `Update` object used for the initial atomic `updateOne` call.** Remove the second `mongoTemplate.updateFirst` call entirely.

Rationale: MongoDB `updateOne` with both array operators (`$inc` on `warehouseDetails.$[wh].qty`) and scalar `$inc` on `maxQuantity` / `availableQuantity` in a single update document applies all field changes in one atomic operation on a single document. No transactions needed (it's a single-document update — already atomic by MongoDB semantics).

**Alternatives considered**:
- Wrap both calls in a reactive transaction via `reactiveMongoTemplate.inTransaction(...)` — rejected: requires MongoDB replica set + higher coordination cost. Single-document atomic update is simpler and correct.
- Move everything into an aggregation pipeline (`$set` with computed arrays) — rejected for THIS ticket: over-engineering for a single-call fix. Already done in `handleStockTransferWithNewEntry` where arrays need to be created.

## Acceptance criteria

- [ ] `doUpdateStockTransferStockAtomic` issues exactly ONE MongoDB update call on the fast path (existing warehouse element).
- [ ] Both warehouse array qty and scalar `maxQuantity` + `availableQuantity` + `updatedAt` are included in that single update.
- [ ] Existing restaurant-business `availMultiplier` (2.0 vs 1.0) logic preserved.
- [ ] Fallback path (no document matched, or warehouse element missing → `handleStockTransferWithNewEntry`) unchanged.
- [ ] `handleSerialDataAdditionIfNeeded` still called after the update (for increment path serial-additions).
- [ ] One new unit test verifies: only one `updateOne`/`updateFirst` invocation occurs on the fast path, and the captured update document contains BOTH `warehouseDetails.$[wh].qty` and `maxQuantity` increments.

## Files likely touched

- `MongoDbService/src/main/java/com/oneshell/mongodb/feature/product/ProductServiceImpl.java` — modify `doUpdateStockTransferStockAtomic` method (lines 2285-2390 approx).
- `MongoDbService/src/test/java/com/oneshell/mongodb/feature/product/ProductServiceImplAtomicTest.java` — new test file (or extend existing `ProductServiceImplTest.java` if preferred).

## Reference patterns (prior art)

- `ProductServiceImpl.handleStockTransferWithNewEntry` (line ~2389) — demonstrates single-call aggregation pipeline pattern using `buildBatchUpdateWithWarehouseAtomic`. NOT what we need here (fast path is simpler) but same function handles both scalar and array updates in one call.
- `ProductServiceImpl.updateOrderStockAtomic` (line ~2709) — similar single-call pattern for order stock. Shows correct style: validate input, single `collection.updateOne`, no multi-step.
- Commit `688af04d` on branch `aiforge/ONE-50a-serial-race-reasoning-50a` — prior fix for serial-race bug in adjacent method. Review for idiom continuity.
- `rag "ProductServiceImpl atomic update"` — returns relevant Mongo update-document patterns.

## Constraints / non-goals

- **DO NOT** touch `handleSerialDataAdditionIfNeeded` — fixed separately on branch `aiforge/ONE-50a`.
- **DO NOT** add negative-stock floor check — that's ONE-50b, out of scope here.
- **DO NOT** refactor `updateWarehouseStockAtomic` (line 2106) — it's a DIFFERENT method, out of scope.
- **OUT OF SCOPE**: MongoDB transactions, aggregation pipeline rewrites, anything beyond merging the two updates into one.

## Test strategy (hint for Sr Dev)

Unit test with Mockito + ArgumentCaptor + StepVerifier:

1. Mock `ReactiveMongoTemplate` + `getCollection()`.
2. Set up a request with `warehouseId`, positive `txnQty`, `stockType="increment"`, a valid productId/businessId.
3. Mock the collection's `updateOne` to return `UpdateResult` with `matchedCount=1, modifiedCount=1` (fast path: warehouse element exists).
4. Invoke `doUpdateStockTransferStockAtomic` (private — use reflection per existing test style).
5. Assert: `updateFirst` on the ReactiveMongoTemplate is NEVER called (only the collection's `updateOne` is).
6. Capture the `Update` document passed to `updateOne`. Assert it contains: `$inc.warehouseDetails.$[wh].qty`, `$inc.maxQuantity`, `$inc.availableQuantity`, `$set.updatedAt`.

This single test catches the bug (two-call pattern) by asserting only one update invocation happens on the fast path.

---

**Sr Developer**: read this file, `rag` relevant idioms, then write `docs/breakdowns/ONE-53.md` with numbered sub-tasks (expect 2-3 sub-tasks: the code change, the test, possibly a cleanup). Commit breakdown on branch `aiforge/ONE-53` in MongoDbService repo. Post ticket comment ending `READY_FOR_DEV`.

**Developer**: after breakdown exists, check out `aiforge/ONE-53`, implement each sub-task with its unit test, commit per sub-task, push, open PR. Final comment `READY_FOR_REVIEW`.
