#!/usr/bin/env bash
# scripts/stock-transfer-fix.sh — Dispatch stock-transfer bug-fix ticket to
# BOTH Sr Dev agents (Reasoning gemma-4-31b + Coder qwen-coder-next).
# Sequential due to Mac Studio RAM. Each produces aiforge/ONE-50-*-{suffix}.
set -uo pipefail

SSH_HOST="${1:-manikanta@192.168.70.185}"
TURNS=300

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

declare -a RUNS=(
  # model|ctx|suffix|name
  "gemma-4-31b-it|131072|reasoning|Sr Dev (Reasoning)"
  "qwen3-coder-next|65536|coder|Sr Dev (Coder)"
)

PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<'EOF'
You are the Senior Developer on AIForgeCrew. Fix three VALIDATED stock-transfer bugs in MongoDbService. Produce a PR branch with the fixes AND new unit tests. End-to-end in ONE session.

TICKET: ONE-50 — Stock transfer critical bug fixes (<SUFFIX> run)

REPO: /Users/manikanta/codeRepo/MongoDbService

## MANDATORY LAYER USAGE (verified working now)

STEP 1 — Use aiforge-rag skill via skill_view. The skill is now registered under its real name:
  skill_view(name="aiforge-rag")

Then run these queries via the skill's Python interface (execute_code RagIndex):
  - "stock transfer serial data race"
  - "ProductServiceImpl atomic update"
  - "mongo $addToSet serialData"
  - "negative stock validation"
  - "isFiniteQty txnQty"

CITE each RAG hit path in your ticket comment.

STEP 2 — hindsight_recall BEFORE touching code:
  - "stock transfer fixes"
  - "serial data race mongodb"
  - "negative stock check"

STEP 3 — Read the CODEBASE_INDEX.md section already in your system prompt (it's injected automatically).

## BUGS TO FIX (validated against ProductServiceImpl.java)

### BUG 1 — Serial data race (BLOCKER)
Location: ProductServiceImpl.java handleSerialDataAdditionIfNeeded() ~lines 2615-2681
Problem: `findOne()` → mutate list in Java → `findAndModify` with `update.set("serialData", list)`. Overwrites concurrent writes. Two transactions adding serials race; one's serial is lost.
Fix: Use MongoDB aggregation pipeline OR `$addToSet` / `$push` with `$cond` to merge atomically. No more read-modify-write.

### BUG 2 — Negative stock allowed (BLOCKER)
Location: ProductServiceImpl.java :2290 (and same pattern at :2112 in updateWarehouseStockAtomic)
Problem: `double txnQty = "increment".equals(...) ? request.getTxnQty() : -request.getTxnQty();` then `update.inc("warehouseDetails.$[wh].qty", txnQty)`. No check prevents qty from going negative.
Reference: updateOrderStockAtomic (~:2709-2714) already has `isFiniteQty + < 0` guard. Apply same pattern.
Fix: Before atomic update, validate the decrement won't drive qty below 0. Reject with `Mono.empty()` + warn log (matching existing pattern).

### BUG 3 — Non-atomic multi-step (HIGH)
Location: ProductServiceImpl.java doUpdateStockTransferStockAtomic() warehouse-array update at ~:2346 followed by separate qtyUpdate at ~:2378
Problem: If qty update fails after warehouse array update succeeds, stock becomes inconsistent. Not wrapped in transaction.
Fix: Either (a) wrap in Mongo transaction via `reactiveMongoTemplate.inTransaction(...)` OR (b) move qty fields into the aggregation pipeline so single updateOne handles all array + scalar updates atomically.

## NON-NEGOTIABLES

- Branch name: aiforge/ONE-50-stock-transfer-fixes-<SUFFIX>
- Tests MUST be added for each bug:
  1. Concurrent serial data adds (two simultaneous transfers with overlapping serials — assert both are retained)
  2. Negative stock rejection (decrement > available qty — assert Mono.empty + no DB change)
  3. Non-atomic recovery (qty update fails mid-flow — assert warehouse array is also rolled back OR update is retried)
- All existing tests must still pass. Run `./mvnw test -Dtest=ProductServiceImpl* -pl . -o` or equivalent.
- Code style: match existing reactive patterns (Mono/Flux, no .block()).
- No cosmetic renames. Focus on the 3 bugs only.

## EXECUTION STEPS (complete each in order)

1. cd /Users/manikanta/codeRepo/MongoDbService
2. git checkout master && git pull origin master
3. git checkout -b aiforge/ONE-50-stock-transfer-fixes-<SUFFIX>
4. STEP 1-3 above (aiforge-rag + hindsight + CODEBASE_INDEX) — do NOT read source files before this
5. Apply Fix 1 (serial race). Write unit test. Run test.
6. Apply Fix 2 (negative stock). Write unit test. Run test.
7. Apply Fix 3 (non-atomic). Write unit test. Run test.
8. Run full test suite: ./mvnw -pl . test -Dtest=ProductServiceImpl* || mvn test (if mvnw missing)
9. When green: git add -A && git commit -m "fix(stock-transfer): serial race + negative qty + atomic multi-step (<SUFFIX>)"
10. git push -u origin aiforge/ONE-50-stock-transfer-fixes-<SUFFIX>
11. gh pr create --base master --title "ONE-50 Stock transfer fixes (<SUFFIX>)" --body "Fixes: serial race, negative stock, non-atomic. See commit for test coverage." --assignee @me
12. Print on final line: COMMIT_SHA=<sha> PR_URL=<url>

## DELIVERABLE SUMMARY (print at end of session)

```
=== ONE-50 <SUFFIX> SUMMARY ===
  RAG hits cited: N
  Hindsight hits: N
  Bugs fixed: 1? 2? 3?
  Tests added: N new tests
  Tests passing: X/Y total in module
  Branch: aiforge/ONE-50-stock-transfer-fixes-<SUFFIX>
  Commit: <sha>
  PR: <url>
```

Commit IMMEDIATELY when tests pass. Do not keep iterating after green.
You have 300 turns. No wall clock limit.
EOF

for entry in "${RUNS[@]}"; do
  IFS='|' read -r MODEL CTX SUFFIX AGENT_NAME <<< "$entry"
  BRANCH="aiforge/ONE-50-stock-transfer-fixes-${SUFFIX}"
  echo "============================================================"
  echo " $AGENT_NAME fix-run — model=$MODEL ctx=$CTX"
  echo "============================================================"

  remote "\$HOME/.lmstudio/bin/lms unload --all 2>&1 | tail -2; sleep 3"
  LOAD=$(remote "\$HOME/.lmstudio/bin/lms load -y '$MODEL' -c $CTX --gpu max 2>&1 | tail -3" || true)
  echo "  load: $(echo "$LOAD" | tr '\n' ' ' | tail -c 120)"
  if ! echo "$LOAD" | grep -q "loaded successfully\|To use the model"; then
    echo "  SKIP $MODEL — load failed"
    continue
  fi

  # Clean target branch if exists
  remote "cd ~/codeRepo/MongoDbService && git checkout master 2>&1 | tail -1 && git branch -D '$BRANCH' 2>/dev/null || true"

  PROMPT_REMOTE="/tmp/stock-transfer-fix-${SUFFIX}.txt"
  sed "s/<SUFFIX>/$SUFFIX/g" "$PROMPT_LOCAL" > "${PROMPT_LOCAL}.sub"
  scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "${PROMPT_LOCAL}.sub" "$SSH_HOST:$PROMPT_REMOTE"
  rm -f "${PROMPT_LOCAL}.sub"

  HERMES_LOG="/tmp/hermes-stock-fix-${SUFFIX}.log"
  echo "  launching hermes (max-turns=$TURNS, NO gtimeout)..."
  START=$(date +%s)
  remote "cd ~/codeRepo/MongoDbService && ~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
  END=$(date +%s); WALL=$((END - START))
  echo "  wall: ${WALL}s"

  # Collect metrics
  BRANCH_FOUND=$(remote "cd ~/codeRepo/MongoDbService && git branch -a 2>/dev/null | grep -o '$BRANCH' | head -1" || true)
  SHA=$(remote "cd ~/codeRepo/MongoDbService && git log -1 --format=%H '$BRANCH' 2>/dev/null" || echo "")
  PR_URL=$(remote "cd ~/codeRepo/MongoDbService && gh pr list --head '$BRANCH' --json url --jq '.[0].url' 2>/dev/null" || echo "")
  CHANGED=$(remote "cd ~/codeRepo/MongoDbService && git diff --stat master..'$BRANCH' 2>/dev/null | tail -1" || true)

  echo "  branch: $BRANCH_FOUND"
  echo "  sha:    ${SHA:0:8}"
  echo "  pr:     $PR_URL"
  echo "  diff:   $CHANGED"

  # Capture session stats + tool counts
  LATEST_SESSION=$(remote "ls -t ~/.hermes/sessions/session_202604*.json | head -1")
  STATS=$(remote "python3 <<PYEOF
import json, sys
from collections import Counter
s = json.load(open('$LATEST_SESSION'))
msgs = s.get('messages', [])
tools = [tc.get('function',{}).get('name','?') for m in msgs if m.get('role')=='assistant' and m.get('tool_calls') for tc in m['tool_calls']]
print(f\"msgs={len(msgs)} asst={sum(1 for m in msgs if m.get('role')=='assistant')} tools={len(tools)}\")
print(f\"breakdown={dict(Counter(tools))}\")
PYEOF")
  echo "  session: $STATS"

  echo
done

rm -f "$PROMPT_LOCAL"
echo "=============== stock transfer fix done ==============="
