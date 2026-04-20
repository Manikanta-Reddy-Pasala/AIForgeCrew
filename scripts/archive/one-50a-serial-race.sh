#!/usr/bin/env bash
# ONE-50a — single-bug ticket: serial data race only. Tightly scoped.
set -uo pipefail

SSH_HOST="${1:-manikanta@192.168.70.185}"
MODEL="${MODEL:-gemma-4-31b-it}"
CTX="${CTX:-131072}"
SUFFIX="${SUFFIX:-reasoning-50a}"
BRANCH="aiforge/ONE-50a-serial-race-${SUFFIX}"
TURNS=150

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

echo "============================================================"
echo " ONE-50a SERIAL RACE — $MODEL ctx=$CTX"
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
You are Sr Developer. Fix EXACTLY ONE bug. Commit. Push. PR. Stop.

TICKET: ONE-50a — Serial data race in handleSerialDataAdditionIfNeeded

REPO: /Users/manikanta/codeRepo/MongoDbService

## THE BUG (only this one)

File: src/main/java/com/oneshell/mongodb/feature/product/ProductServiceImpl.java
Method: handleSerialDataAdditionIfNeeded (around lines 2615-2681)

Pattern (buggy):
  mongoTemplate.findOne(query) → mutate List<SerialDataDao> in Java → findAndModify with `update.set("serialData", newList)`

Problem: `update.set` overwrites the ENTIRE serialData array. If two transactions concurrently add different serials, the second one clobbers the first.

## THE FIX (only this one)

Replace the read-modify-write with an atomic aggregation pipeline or `$addToSet`/`$push` with `$cond`. One MongoDB call, no Java-side list mutation.

Reference implementations:
  rag "ProductServiceImpl aggregation pipeline warehouseDetails"
  rag "mongo addToSet array with condition"

## STEPS (each is a single action)

1. cd /Users/manikanta/codeRepo/MongoDbService
2. git checkout master && git pull origin master
3. git checkout -b aiforge/ONE-50a-serial-race-<SUFFIX>
4. rag "handleSerialDataAdditionIfNeeded existing implementation"  (one query — cite top hit)
5. Read the method: lines 2615-2690 of ProductServiceImpl.java only
6. Rewrite it: the new method signature stays the same. Body uses pipeline or $addToSet, NO findOne-mutate-set.
7. Add ONE unit test:
     File: src/test/java/com/oneshell/mongodb/feature/product/ProductServiceImplSerialRaceTest.java
     Uses: JUnit 5, Mockito, StepVerifier
     Asserts: concurrent adds of two different serials both persist (mock ReactiveMongoTemplate, verify the aggregation/update doc sent to Mongo contains $addToSet or pipeline stage — NOT set-whole-array)
8. Compile only: `./mvnw compile -pl . -o` (skip full test suite to save time)
9. git add -A && git commit -m "fix(stock-transfer): serial data race (pipeline/addToSet)"
10. git push -u origin aiforge/ONE-50a-serial-race-<SUFFIX>
11. gh pr create --base master --title "ONE-50a Serial data race" --body "Replaces read-modify-write with atomic $addToSet/pipeline. One new test." --assignee @me
12. Print final line: COMMIT=<sha> PR=<url>

## HARD RULES

- DO NOT touch doUpdateStockTransferStockAtomic or any method other than handleSerialDataAdditionIfNeeded.
- DO NOT add more than 1 new test.
- DO NOT run the full test suite — `./mvnw compile` is enough proof the code builds.
- DO NOT re-read unrelated files. Read only the target method + reference warehouseDetails pipeline.
- COMMIT + PUSH + PR immediately after compile succeeds. Do not iterate further.
- If rag/hindsight return nothing useful, move on — don't loop. Max 3 rag calls total.

You have 150 turns. Target completion: 15 min.
EOF

PROMPT_REMOTE="/tmp/one-50a-prompt-${SUFFIX}.txt"
sed "s/<SUFFIX>/$SUFFIX/g" "$PROMPT_LOCAL" > "${PROMPT_LOCAL}.sub"
scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "${PROMPT_LOCAL}.sub" "$SSH_HOST:$PROMPT_REMOTE"
rm -f "${PROMPT_LOCAL}.sub" "$PROMPT_LOCAL"

HERMES_LOG="/tmp/hermes-one-50a-${SUFFIX}.log"
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
  remote "cd ~/codeRepo/MongoDbService && git checkout '$BRANCH' 2>&1 | tail -1 && git add -A && git commit -m 'WIP $SUFFIX (incomplete)' 2>&1 | tail -2" || true
  SHA=$(remote "cd ~/codeRepo/MongoDbService && git log '$BRANCH' ^master --oneline 2>/dev/null | head -1" || echo "")
  CHANGED=$(remote "cd ~/codeRepo/MongoDbService && git diff --stat master..'$BRANCH' 2>/dev/null | tail -1" || true)
fi

echo "  branch: $BRANCH_FOUND"
echo "  sha:    $SHA"
echo "  pr:     $PR_URL"
echo "  diff:   $CHANGED"
echo "done"
