# ONE-52 Breakdown — Stock transfer non-atomic warehouse+qty multi-step update

## Problem Summary
The `doUpdateStockTransferStockAtomic` method in `ProductServiceImpl.java` currently performs two separate MongoDB update operations when updating stock for an existing warehouse element:
1. An `updateOne` call to increment/decrement the specific warehouse quantity in the `warehouseDetails` array.
2. A separate `updateFirst` call to update scalar product-level totals (`maxQuantity`, `availableQuantity`) and the `updatedAt` timestamp.

This non-atomic sequence creates a race condition/consistency risk: if the second call fails, the warehouse-specific stock is updated but the overall product totals are stale.

## Implementation Plan

### 1. Merge Scalar Updates into Atomic `updateOne`
Modify the logic in `doUpdateStockTransferStockAtomic` to include scalar field increments (`maxQuantity`, `availableQuantity`) and the `updatedAt` timestamp update within the same MongoDB `Update` object used for the warehouse array update.

- **Task**: Locate the `updateOne` call (approx lines 2285-2390).
- **Task**: Add `$inc` operations for `maxQuantity` and `availableQuantity`.
- **Task**: Add `$set` operation for `updatedAt`.
- **Task**: Remove the subsequent `mongoTemplate.updateFirst` call.
- **Verification**: Ensure the `availMultiplier` logic (1.0 vs 2.0) is correctly applied to the scalar increments.

**Test Case**:
- **Scenario**: Execute a stock transfer for an existing warehouse element.
- **Expected Result**: Only one MongoDB update call is issued. The captured `Update` document contains both the array-filter based quantity change and the scalar product total changes.

### 2. Implement Verification Unit Test
Create or extend a test class to verify the atomicity of the operation using Mockito `ArgumentCaptor`.

- **Task**: Create/Update `ProductServiceImplAtomicTest.java` (or similar).
- **Task**: Mock `ReactiveMongoTemplate` and the underlying MongoDB collection.
- **Task**: Use `ArgumentCaptor` to capture the `Update` object passed to `collection.updateOne`.
- **Task**: Assert that `mongoTemplate.updateFirst` is never called on the fast path.
- **Task**: Assert that the captured `Update` object contains:
    - `$inc` for `warehouseDetails.$[wh].qty`
    - `$inc` for `maxQuantity`
    - `$inc` for `availableQuantity`
    - `$set` for `updatedAt`

**Test Case**:
- **Scenario**: Mock a successful "fast path" update (warehouse element exists).
- **Expected Result**: `verify(mongoTemplate, times(0)).updateFirst(...)` and the captured update document matches the expected atomic structure.

### 3. Regression & Cleanup
Verify that other paths remain unaffected and remove any now-unused local variables related to the second update call.

- **Task**: Verify `handleStockTransferWithNewEntry` (fallback path) remains unchanged.
- **Task**: Verify `handleSerialDataAdditionIfNeeded` is still called after the atomic update.
- **Task**: Clean up unused imports or variables in `ProductServiceImpl.java`.

**Test Case**:
- **Scenario**: Execute a stock transfer where the warehouse element does NOT exist.
- **Expected Result**: The logic correctly falls back to `handleStockTransferWithNewEntry` and behaves as before.
