#!/usr/bin/env bash
# ONE-50 stock-transfer bug-fix v2 — uses the new `rag` CLI wrapper, simplified
# prompt, clean repo state per run.
set -uo pipefail

SSH_HOST="${1:-manikanta@192.168.70.185}"
TURNS=300

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

declare -a RUNS=(
  "gemma-4-31b-it|131072|reasoning-v2"
  "qwen3-coder-next|65536|coder-v2"
)

PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<'EOF'
You are Sr Developer on AIForgeCrew. Fix 3 VALIDATED stock-transfer bugs in MongoDbService. Produce a PR branch with fixes + tests. End-to-end, one session.

TICKET: ONE-50 — Stock transfer fixes (<SUFFIX>)

REPO: /Users/manikanta/codeRepo/MongoDbService

## RETRIEVAL LAYERS (use these FIRST, before reading source)

Tool 1 — RAG via `rag` CLI (terminal tool):
  rag "handleSerialDataAdditionIfNeeded atomic pipeline"
  rag "doUpdateStockTransferStockAtomic"
  rag "updateWarehouseStockAtomic negative qty"
  rag "updateOrderStockAtomic isFiniteQty validation"
  rag "mongo addToSet serialData"

Each command returns top-5 method-level chunks from MongoDbService + related repos. Cite the `[source]` paths in your final summary.

Tool 2 — hindsight_recall × 3:
  hindsight_recall("stock transfer fixes")
  hindsight_recall("mongodb aggregation pipeline")
  hindsight_recall("reactive transaction pattern")
  Log results.

Tool 3 — CODEBASE_INDEX.md is already injected in your system prompt. Refer to it for module layout.

DO NOT read source files until you have run the 5 `rag` queries and 3 `hindsight_recall` calls.

## BUGS TO FIX (verified against real code)

### BUG 1 — Serial race (BLOCKER)
  File: src/main/java/com/oneshell/mongodb/feature/product/ProductServiceImpl.java
  Method: handleSerialDataAdditionIfNeeded() lines ~2615-2681
  Symptom: findOne → mutate List<SerialDataDao> in Java → findAndModify with `update.set("serialData", list)` overwrites concurrent writers.
  Fix: rewrite using MongoDB aggregation pipeline OR `$addToSet`/`$push` with `$cond`. No read-modify-write-set of the full array.

### BUG 2 — Negative stock (BLOCKER)
  Methods: doUpdateStockTransferStockAtomic (line 2285+), updateWarehouseStockAtomic (line 2106+)
  Symptom: decrement sign-flip (`-request.getTxnQty()`) then unconditional `update.inc("warehouseDetails.$[wh].qty", txnQty)`. Can drive qty < 0.
  Reference pattern: updateOrderStockAtomic at ~line 2709 already has `isFiniteQty + < 0` guard. Apply it.
  Fix: reject with `Mono.empty()` + warn log when decrement would cross zero.

### BUG 3 — Non-atomic multi-step (HIGH)
  Method: doUpdateStockTransferStockAtomic
  Symptom: warehouse-array update at ~line 2346 (collection.updateOne) followed by separate qty update at ~line 2378 (mongoTemplate.updateFirst). Two mongo calls, no transaction.
  Fix: either (a) wrap both in reactiveMongoTemplate session-transaction OR (b) merge quantity writes into the aggregation pipeline so a single updateOne handles everything.

## REQUIRED DELIVERABLES

- Branch: aiforge/ONE-50-stock-transfer-fixes-<SUFFIX>
- 3 NEW unit tests at src/test/java/com/oneshell/mongodb/feature/product/ProductServiceImplTest.java (one per bug). Use JUnit 5 + Mockito + reactor StepVerifier.
- Build/test: `./mvnw test -Dtest=ProductServiceImplTest -pl . -o` (fall back to `mvn test` if mvnw fails)
- All existing tests must pass.

## STEPS

1. cd /Users/manikanta/codeRepo/MongoDbService
2. git checkout master && git pull origin master
3. git checkout -b aiforge/ONE-50-stock-transfer-fixes-<SUFFIX>
4. Run the 5 `rag` queries + 3 `hindsight_recall` calls. Log hits + facts.
5. Read ProductServiceImpl.java method blocks for handleSerialDataAdditionIfNeeded, doUpdateStockTransferStockAtomic, updateWarehouseStockAtomic, updateOrderStockAtomic.
6. Fix BUG 1 → write test for BUG 1 → run just that test → fix as needed.
7. Fix BUG 2 → write test → run → fix.
8. Fix BUG 3 → write test → run → fix.
9. Run full ProductServiceImplTest class. All green.
10. git add -A && git commit -m "fix(stock-transfer): serial race + negative qty + atomic multi-step (<SUFFIX>)"
11. git push -u origin aiforge/ONE-50-stock-transfer-fixes-<SUFFIX>
12. gh pr create --base master --title "ONE-50 Stock transfer fixes (<SUFFIX>)" --body "Fixes 3 validated bugs with 3 new unit tests." --assignee @me

## FINAL SUMMARY BLOCK (print on last line)

```
=== ONE-50 <SUFFIX> SUMMARY ===
  RAG hits cited: N
  Hindsight facts: N
  Bugs fixed: X/3
  Tests added: N
  Build result: N passed / M total
  Branch: aiforge/ONE-50-stock-transfer-fixes-<SUFFIX>
  Commit: <sha>
  PR: <url>
```

COMMIT IMMEDIATELY when ProductServiceImplTest class passes. Do NOT keep re-reading after tests green.

You have 300 turns. No wall clock.
EOF

for entry in "${RUNS[@]}"; do
  IFS='|' read -r MODEL CTX SUFFIX <<< "$entry"
  BRANCH="aiforge/ONE-50-stock-transfer-fixes-${SUFFIX}"
  echo "============================================================"
  echo " $MODEL ctx=$CTX branch=$BRANCH"
  echo "============================================================"

  remote "\$HOME/.lmstudio/bin/lms unload --all 2>&1 | tail -2; sleep 3"
  LOAD=$(remote "\$HOME/.lmstudio/bin/lms load -y '$MODEL' -c $CTX --gpu max 2>&1 | tail -3" || true)
  echo "  load: $(echo "$LOAD" | tr '\n' ' ' | tail -c 120)"
  if ! echo "$LOAD" | grep -q "loaded successfully\|To use the model"; then
    echo "  SKIP"; continue
  fi

  remote "cd ~/codeRepo/MongoDbService && git checkout master 2>&1 | tail -1 && git reset --hard origin/master 2>&1 | tail -1 && git branch -D '$BRANCH' 2>/dev/null; git status -sb | head -1"

  PROMPT_REMOTE="/tmp/stock-transfer-fix-v2-${SUFFIX}.txt"
  sed "s/<SUFFIX>/$SUFFIX/g" "$PROMPT_LOCAL" > "${PROMPT_LOCAL}.sub"
  scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "${PROMPT_LOCAL}.sub" "$SSH_HOST:$PROMPT_REMOTE"
  rm -f "${PROMPT_LOCAL}.sub"

  HERMES_LOG="/tmp/hermes-stock-fix-v2-${SUFFIX}.log"
  START=$(date +%s)
  echo "  launching hermes (max-turns=$TURNS)..."
  remote "cd ~/codeRepo/MongoDbService && ~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
  END=$(date +%s); WALL=$((END - START))
  echo "  wall: ${WALL}s"

  BRANCH_FOUND=$(remote "cd ~/codeRepo/MongoDbService && git branch -a 2>/dev/null | grep -o '$BRANCH' | head -1" || true)
  SHA=$(remote "cd ~/codeRepo/MongoDbService && git log '$BRANCH' ^master --oneline 2>/dev/null | head -1" || echo "")
  PR_URL=$(remote "cd ~/codeRepo/MongoDbService && gh pr list --head '$BRANCH' --json url --jq '.[0].url' 2>/dev/null" || echo "")
  CHANGED=$(remote "cd ~/codeRepo/MongoDbService && git diff --stat master..'$BRANCH' 2>/dev/null | tail -1" || true)

  if [[ -n "$BRANCH_FOUND" && -z "$SHA" ]]; then
    remote "cd ~/codeRepo/MongoDbService && git checkout '$BRANCH' 2>&1 | tail -1 && git add -A && git commit -m 'WIP $SUFFIX stock transfer (incomplete)' 2>&1 | tail -2" || true
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
  echo
done

rm -f "$PROMPT_LOCAL"
echo "=============== v2 fix-run done ==============="
