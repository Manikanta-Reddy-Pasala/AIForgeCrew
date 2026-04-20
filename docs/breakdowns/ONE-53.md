# ONE-53 — Stock transfer non-atomic warehouse+qty multi-step update

## Breakdown

The goal is to merge two separate MongoDB update calls in `doUpdateStockTransferStockAtomic` into a single atomic operation to prevent data inconsistency when the second call fails.

### Sub-tasks

1. **Merge scalar quantity updates into the primary atomic update**
   - Modify `doUpdateStockTransferStockAtomic` in `ProductServiceImpl.java`.
   - Move the logic from `qtyUpdate` (lines 2370-2373) into the main `update` object used for `collection.updateOne`.
   - Ensure `maxQuantity`, `availableQuantity` (with `availMultiplier`), and `updatedAt` are all included in the single `$inc`/`$set` operation.
   - Remove the second `mongoTemplate.updateFirst` call (lines 2375-2377).
   - **Test Case**: `testDoUpdateStockTransferStockAtomic_SingleCallFastPath` — Verify that only one MongoDB update is executed and it contains both warehouse-level and product-level quantity increments.

2. **Implement unit test for atomicity verification**
   - Create or extend `ProductServiceImplAtomicTest.java`.
   - Use Mockito `ArgumentCaptor` to capture the update document passed to `collection.updateOne`.
   - Assert that the captured document contains:
     - `$inc` for `warehouseDetails.$[wh].qty`
     - `$inc` for `maxQuantity`
     - `$inc` for `availableQuantity`
     - `$set` for `updatedAt`
   - Assert that `mongoTemplate.updateFirst` is never called on the fast path.
   - **Test Case**: `testDoUpdateStockTransferStockAtomic_VerifyUpdateDocumentContent` — Specifically validate the structure of the MongoDB update document.

3. **Verification and Cleanup**
   - Verify that `handleSerialDataAdditionIfNeeded` is still called correctly after the single atomic update.
   - Ensure that the fallback path (`handleStockTransferWithNewEntry`) remains untouched and functional.
   - **Test Case**: `testDoUpdateStockTransferStockAtomic_FallbackPath` — Verify that if the first update fails to match/modify, it still correctly routes to `handleStockTransferWithNewEntry`.
