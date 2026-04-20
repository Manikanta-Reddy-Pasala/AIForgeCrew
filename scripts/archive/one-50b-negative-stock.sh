#!/usr/bin/env bash
# ONE-50b — single-bug ticket: negative stock validation only.
set -uo pipefail

SSH_HOST="${1:-manikanta@192.168.70.185}"
MODEL="${MODEL:-qwen3-coder-next}"
CTX="${CTX:-65536}"
SUFFIX="${SUFFIX:-coder-50b}"
BRANCH="aiforge/ONE-50b-negative-stock-${SUFFIX}"
TURNS=150

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

echo "============================================================"
echo " ONE-50b NEGATIVE STOCK — $MODEL ctx=$CTX"
echo "============================================================"

remote "\$HOME/.lmstudio/bin/lms unload --all 2>&1 | tail -2; sleep 3"
LOAD=$(remote "\$HOME/.lmstudio/bin/lms load -y '$MODEL' -c $CTX --gpu max 2>&1 | tail -3" || true)
echo "  load: $(echo "$LOAD" | tr '\n' ' ' | tail -c 120)"
if ! echo "$LOAD" | grep -q "loaded successfully\|To use the model"; then
  echo "  SKIP"; exit 1
fi

remote "cd ~/codeRepo/MongoDbService && git checkout master 2>&1 | tail -1 && git reset --hard origin/master 2>&1 | tail -1 && git clean -fdx 2>&1 | tail -1; git branch -D '$BRANCH' 2>/dev/null; git status -sb | head -1"

PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<'EOF'
You are Sr Developer (Coder). Fix EXACTLY ONE bug. Commit. Push. PR. Stop.

TICKET: ONE-50b — Negative stock not rejected on decrement

REPO: /Users/manikanta/codeRepo/MongoDbService

## THE BUG (only this one)

Files + methods:
  src/main/java/com/oneshell/mongodb/feature/product/ProductServiceImpl.java
    - doUpdateStockTransferStockAtomic (line 2285+)
    - updateWarehouseStockAtomic (line 2106+)

Problem: decrement path sign-flips txnQty via `-request.getTxnQty()` then unconditionally `update.inc("warehouseDetails.$[wh].qty", txnQty)`. No check prevents qty from going below 0.

Reference pattern that ALREADY EXISTS in the same file:
  updateOrderStockAtomic (around line 2709-2714) has:
    if (!isFiniteQty(request.getTxnQty()) || request.getTxnQty() < 0) {
        log.warn("[ORDERED-STOCK] rejected — invalid txnQty={} productId={} businessId={}",
                request.getTxnQty(), request.getProductId(), request.getBusinessId());
        return Mono.empty();
    }
  Apply the same pattern to doUpdateStockTransferStockAtomic and updateWarehouseStockAtomic.

## THE FIX

At the top of BOTH target methods, reject invalid/negative txnQty BEFORE any DB work.
Optional (nice-to-have, not required): for decrement with valid qty, add a floor-check that rejects if current warehouse qty would cross zero — can be done via query condition or via `$cond` in pipeline. If simple to add without refactoring, include it. If not, the isFiniteQty + >=0 guard alone counts as this ticket's minimum fix.

Minimum viable fix = the existing isFiniteQty guard copied to both methods.

## STEPS

1. cd /Users/manikanta/codeRepo/MongoDbService
2. git checkout master && git pull origin master
3. git checkout -b aiforge/ONE-50b-negative-stock-<SUFFIX>
4. rag "updateOrderStockAtomic isFiniteQty validation"  (one query — cite top hit)
5. Read ProductServiceImpl.java lines 2095-2400 (the 2 target methods + the reference pattern)
6. Add the guard at top of updateWarehouseStockAtomic + doUpdateStockTransferStockAtomic
7. Add ONE unit test at src/test/java/com/oneshell/mongodb/feature/product/ProductServiceImplNegativeStockTest.java
   Uses: JUnit 5, Mockito, StepVerifier
   Test case: UpdateProductStockQtyRequest with stockType=decrement + txnQty set such that current flow would produce negative. Mock ReactiveMongoTemplate. Assert Mono.empty() returned AND updateOne/updateFirst NOT called.
8. git add -A && git commit -m "fix(stock-transfer): reject negative/invalid qty on decrement"
9. git push -u origin aiforge/ONE-50b-negative-stock-<SUFFIX>
10. gh pr create --base master --title "ONE-50b Negative stock rejection" --body "Applies isFiniteQty + < 0 guard (matching updateOrderStockAtomic pattern) to doUpdateStockTransferStockAtomic and updateWarehouseStockAtomic. One new test." --assignee @me  (if gh missing, skip PR step, that's fine)
11. Print final line: COMMIT=<sha> PR=<url or "skipped">

## HARD RULES

- DO NOT touch handleSerialDataAdditionIfNeeded or any unrelated method.
- DO NOT add more than 1 new test.
- DO NOT refactor for cleanliness — minimum viable fix only.
- DO NOT run the full test suite (no JDK on this host anyway — don't try ./mvnw).
- COMMIT + PUSH immediately after the test file is written. No further iteration.
- Max 2 `rag` calls total. Max 6 file reads.
- If `gh` not found, skip PR step and continue.

You have 150 turns. Target completion: 15 min.
EOF

PROMPT_REMOTE="/tmp/one-50b-prompt-${SUFFIX}.txt"
sed "s/<SUFFIX>/$SUFFIX/g" "$PROMPT_LOCAL" > "${PROMPT_LOCAL}.sub"
scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "${PROMPT_LOCAL}.sub" "$SSH_HOST:$PROMPT_REMOTE"
rm -f "${PROMPT_LOCAL}.sub" "$PROMPT_LOCAL"

HERMES_LOG="/tmp/hermes-one-50b-${SUFFIX}.log"
START=$(date +%s)
echo "  launching hermes (max-turns=$TURNS)..."
remote "cd ~/codeRepo/MongoDbService && ~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
END=$(date +%s); WALL=$((END - START))
echo "  wall: ${WALL}s"

BRANCH_FOUND=$(remote "cd ~/codeRepo/MongoDbService && git branch -a 2>/dev/null | grep -o '$BRANCH' | head -1" || true)
SHA=$(remote "cd ~/codeRepo/MongoDbService && git log '$BRANCH' ^master --oneline 2>/dev/null | head -1" || echo "")
CHANGED=$(remote "cd ~/codeRepo/MongoDbService && git diff --stat master..'$BRANCH' 2>/dev/null | tail -1" || true)

if [[ -n "$BRANCH_FOUND" && -z "$SHA" ]]; then
  remote "cd ~/codeRepo/MongoDbService && git checkout '$BRANCH' 2>&1 | tail -1 && git add -A && git commit -m 'WIP $SUFFIX (incomplete)' 2>&1 | tail -2" || true
  SHA=$(remote "cd ~/codeRepo/MongoDbService && git log '$BRANCH' ^master --oneline 2>/dev/null | head -1" || echo "")
  CHANGED=$(remote "cd ~/codeRepo/MongoDbService && git diff --stat master..'$BRANCH' 2>/dev/null | tail -1" || true)
fi

echo "  branch: $BRANCH_FOUND"
echo "  sha:    $SHA"
echo "  diff:   $CHANGED"
echo "done"
