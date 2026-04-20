#!/usr/bin/env bash
# scripts/stock-transfer-fix-coder.sh — qwen-only rerun for ONE-50 after fixing
# the "uncommitted carry-over" state.
set -uo pipefail
SSH_HOST="${1:-manikanta@192.168.70.185}"
MODEL="qwen3-coder-next"
CTX=65536
SUFFIX="coder"
BRANCH="aiforge/ONE-50-stock-transfer-fixes-${SUFFIX}"
TURNS=300

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

echo "============================================================"
echo " Sr Dev (Coder) fix-run — model=$MODEL ctx=$CTX"
echo "============================================================"

remote "\$HOME/.lmstudio/bin/lms unload --all 2>&1 | tail -2; sleep 3"
LOAD=$(remote "\$HOME/.lmstudio/bin/lms load -y '$MODEL' -c $CTX --gpu max 2>&1 | tail -3" || true)
echo "  load: $(echo "$LOAD" | tr '\n' ' ' | tail -c 120)"
if ! echo "$LOAD" | grep -q "loaded successfully\|To use the model"; then
  echo "  SKIP"; exit 1
fi

# Ensure master is CLEAN (no stray uncommitted changes from prior run)
remote "cd ~/codeRepo/MongoDbService && git checkout master 2>&1 | tail -1 && git checkout -- . 2>&1 && git branch -D '$BRANCH' 2>/dev/null; git status -sb | head -3"

PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<'EOF'
You are Sr Developer (Coder) on AIForgeCrew. Fix three VALIDATED stock-transfer bugs in MongoDbService. Produce a PR branch with the fixes AND new unit tests. End-to-end in ONE session.

TICKET: ONE-50 — Stock transfer critical bug fixes (coder run)

REPO: /Users/manikanta/codeRepo/MongoDbService

## MANDATORY LAYERS (all three working — use them)

STEP 1 — aiforge-rag skill (registered now, dir rename fix applied):
  skill_view(name="aiforge-rag")

Then execute_code Python for each query:
```python
from aiforge_core.rag import RagIndex
from pathlib import Path
idx = RagIndex(Path("/Users/manikanta/AIForgeCrew"))
for q in ["ProductServiceImpl atomic update", "mongo addToSet serialData", "isFiniteQty txnQty", "serial data race", "negative stock validation"]:
    print(f"=== {q} ===")
    for h in idx.query(q, top_k=3):
        print(f"  [{h.source}] {h.text[:180]}")
```
Run this BEFORE reading any Java source. Cite at least 5 hits in your final summary.

STEP 2 — hindsight_recall × 3:
  hindsight_recall("stock transfer fixes")
  hindsight_recall("serial data race mongodb")
  hindsight_recall("negative stock check")

STEP 3 — CODEBASE_INDEX.md is in your system prompt. Refer to it.

## BUGS TO FIX (validated against ProductServiceImpl.java)

### BUG 1 — Serial data race (BLOCKER)
File: src/main/java/com/oneshell/mongodb/feature/product/ProductServiceImpl.java
Method: handleSerialDataAdditionIfNeeded() ~lines 2615-2681
Issue: findOne() → mutate list in Java → findAndModify with `update.set("serialData", list)`. Concurrent writes lose one txn's serials.
Fix: Replace read-modify-write with MongoDB aggregation pipeline (preferred) OR `$addToSet` with conditional merge. No raw `update.set("serialData", ...)` of whole array.

### BUG 2 — Negative stock allowed (BLOCKER)
Method: doUpdateStockTransferStockAtomic (line 2285+) and updateWarehouseStockAtomic (line 2106+)
Issue: `double txnQty = "decrement" ? -request.getTxnQty() : ...` → `update.inc("warehouseDetails.$[wh].qty", txnQty)` with no floor check. Can drive qty below 0.
Reference: updateOrderStockAtomic (~:2709) uses `isFiniteQty + < 0` rejection. Copy that pattern.
Fix: Before `collection.updateOne(...)`, validate decrement won't cross 0. For decrement, require that current warehouse qty ≥ request.getTxnQty() — OR use aggregation `$cond` to abort if decrement would go negative.

### BUG 3 — Non-atomic multi-step (HIGH)
Method: doUpdateStockTransferStockAtomic, warehouse update at ~:2346 + separate qty update at ~:2378
Issue: Two mongo calls, no transaction. Qty update can fail after warehouse write, leaving inconsistent state.
Fix: Either (a) wrap in reactiveMongoTemplate session-transaction OR (b) merge qty fields into the aggregation pipeline so updateOne applies both.

## NON-NEGOTIABLES

- Branch: aiforge/ONE-50-stock-transfer-fixes-coder
- 3 NEW unit tests (one per bug). Put them in the existing ProductServiceImpl test class (src/test/java/.../ProductServiceImplTest.java or create if absent).
- All existing tests must still pass. Run: `./mvnw test -Dtest=ProductService* -pl . -o` (if mvnw works) or `mvn test` as fallback.
- Reactive style. No .block() outside tests.
- COMMIT IMMEDIATELY when tests are green. Then push + open PR.

## EXECUTION ORDER (do not skip)

1. cd /Users/manikanta/codeRepo/MongoDbService
2. git checkout master && git pull origin master
3. git checkout -b aiforge/ONE-50-stock-transfer-fixes-coder
4. skill_view("aiforge-rag") + execute_code RagIndex queries (the 5 above) — cite hits
5. hindsight_recall × 3 — log results
6. Read handleSerialDataAdditionIfNeeded, doUpdateStockTransferStockAtomic, updateWarehouseStockAtomic, updateOrderStockAtomic
7. Fix BUG 1, write test, run test
8. Fix BUG 2, write test, run test
9. Fix BUG 3, write test, run test
10. Run full ProductService test class — all green
11. git add -A && git commit -m "fix(stock-transfer): serial race + negative qty + atomic multi-step"
12. git push -u origin aiforge/ONE-50-stock-transfer-fixes-coder
13. gh pr create --base master --title "ONE-50 Stock transfer fixes (coder)" --body "$(cat <<SUMMARY
Fixes 3 validated stock-transfer bugs.
  1. Serial data race — replaced read-modify-write with pipeline/addToSet.
  2. Negative stock — added isFiniteQty + floor-check guard.
  3. Non-atomic — merged into single aggregation pipeline.
Includes 3 new unit tests.
SUMMARY
)" --assignee @me

## FINAL SUMMARY BLOCK (print at end)

```
=== ONE-50 coder SUMMARY ===
  RAG hits cited: N
  Hindsight recall hits: N
  Bugs fixed: X/3
  Tests added: N
  Full suite: X/Y passing
  Branch: aiforge/ONE-50-stock-transfer-fixes-coder
  Commit: <sha>
  PR: <url>
```

Commit IMMEDIATELY when green. Do NOT keep iterating.
You have 300 turns. No wall clock limit.
EOF

PROMPT_REMOTE="/tmp/stock-transfer-fix-coder.txt"
scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$PROMPT_LOCAL" "$SSH_HOST:$PROMPT_REMOTE"
rm -f "$PROMPT_LOCAL"

HERMES_LOG="/tmp/hermes-stock-fix-coder.log"
START=$(date +%s)
echo "  launching hermes (max-turns=$TURNS, NO gtimeout)..."
remote "cd ~/codeRepo/MongoDbService && ~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
END=$(date +%s); WALL=$((END - START))
echo "  wall: ${WALL}s"

# Collect
BRANCH_FOUND=$(remote "cd ~/codeRepo/MongoDbService && git branch -a 2>/dev/null | grep -o '$BRANCH' | head -1" || true)
SHA=$(remote "cd ~/codeRepo/MongoDbService && git log aiforge/ONE-50-stock-transfer-fixes-coder ^master --oneline 2>/dev/null | head -1" || echo "")
PR_URL=$(remote "cd ~/codeRepo/MongoDbService && gh pr list --head '$BRANCH' --json url --jq '.[0].url' 2>/dev/null" || echo "")
CHANGED=$(remote "cd ~/codeRepo/MongoDbService && git diff --stat master..'$BRANCH' 2>/dev/null | tail -1" || true)

# If no commits on branch but uncommitted diff exists, capture as WIP
if [[ -n "$BRANCH_FOUND" && -z "$SHA" ]]; then
  remote "cd ~/codeRepo/MongoDbService && git checkout '$BRANCH' 2>&1 | tail -1 && git add -A && git commit -m 'WIP qwen-coder-next stock transfer attempt (incomplete)' 2>&1 | tail -2" || true
  SHA=$(remote "cd ~/codeRepo/MongoDbService && git log '$BRANCH' ^master --oneline 2>/dev/null | head -1" || echo "")
  CHANGED=$(remote "cd ~/codeRepo/MongoDbService && git diff --stat master..'$BRANCH' 2>/dev/null | tail -1" || true)
fi

LATEST_SESSION=$(remote "ls -t ~/.hermes/sessions/session_202604*.json | head -1")
STATS=$(remote "python3 <<PYEOF
import json
from collections import Counter
s = json.load(open('$LATEST_SESSION'))
msgs = s.get('messages', [])
tools = [tc.get('function',{}).get('name','?') for m in msgs if m.get('role')=='assistant' and m.get('tool_calls') for tc in m['tool_calls']]
print(f\"msgs={len(msgs)} asst={sum(1 for m in msgs if m.get('role')=='assistant')} tools={len(tools)}\")
print(f\"breakdown={dict(Counter(tools))}\")
PYEOF")

echo "  branch:  $BRANCH_FOUND"
echo "  sha:     $SHA"
echo "  pr:      $PR_URL"
echo "  diff:    $CHANGED"
echo "  session: $STATS"
echo "done"
