#!/usr/bin/env bash
# scripts/boi-v2-direct.sh — direct hermes invocation (no Paperclip) for BOI v2
# model evaluation. Runs hermes chat as a single long session per model with
# --max-turns 300 + 3600s timeout, not heartbeat-driven. Bypasses the
# Paperclip-wake issue where hermes exits after ~1 turn.
set -uo pipefail

declare -a CANDIDATES=(
  "qwen3-coder-next|65536|qwen"
  "gemma-4-31b-it|32768|gemma"
)

SSH_HOST="${1:-manikanta@192.168.70.185}"
OUT_CSV="${OUT_CSV:-docs/eval/boi-v2-bench.csv}"
TURNS="${TURNS:-300}"
SECS="${SECS:-3600}"

mkdir -p "$(dirname "$OUT_CSV")"
[[ -f "$OUT_CSV" ]] || echo "model,ctx,branch,tests_passed,tests_total,file_created,test_file_created,commit_sha,pr_url,wall_seconds,final_status" > "$OUT_CSV"

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

build_prompt() {
  local SUFFIX="$1"
  cat <<EOF
You are Sr Developer on AIForgeCrew. Execute this ticket end-to-end in ONE session. Do NOT return until all steps are done.

TICKET: ONE-48 — Bank of India OCR parser v2

REPO: /Users/manikanta/codeRepo/PosPythonBackend (current directory)

FILES ALREADY STAGED (read these first):
  - tests/fixtures/BOI.pdf              (sample statement)
  - tests/fixtures/boi_expected.json    (golden output, 12 txns hand-verified)
  - tests/fixtures/ONE-48-test-spec.py  (required test file, 12 tests you MUST copy over verbatim)

TARGET FILES TO CREATE/REPLACE:
  - app/util/boi_bank_handler.py
  - tests/util/test_boi_bank_handler.py (copy from tests/fixtures/ONE-48-test-spec.py)
  - tests/util/conftest.py or pytest.ini (to resolve the app.util.boi_bank_handler import)

ACCEPTANCE: handler exposes parse(pdf_path: str) -> dict with:
  metadata: {bank_name, branch, account_name, account_number, customer_id, account_type, ifsc_code, statement_period_start, statement_period_end, statement_generated_at}
  transactions: [{serial, txn_date (ISO YYYY-MM-DD), description, chequeNo (nullable), transactionAmount, transactionDirection ("CR"|"DR"), balance}, ...]

NON-NEGOTIABLES:
  - direction inferred from Withdrawal vs Deposits column (not hardcoded)
  - Indian lakh/crore format parsing (1,14,000.00 -> 114000.00)
  - preserve negative balances (Cash Credit)
  - chequeNo = null when blank, not empty string
  - dates ISO YYYY-MM-DD, not DD-MM-YYYY

EXECUTION STEPS (do each in order, do not stop until done):
1. cd /Users/manikanta/codeRepo/PosPythonBackend
2. git checkout master && git pull origin master
3. git checkout -b aiforge/ONE-48-${SUFFIX}-boi-v2
4. cp tests/fixtures/ONE-48-test-spec.py tests/util/test_boi_bank_handler.py
5. Write the new handler at app/util/boi_bank_handler.py (replace old 103-line version)
6. Add tests/util/conftest.py or root pytest.ini so import resolves
7. Run: ./venv/bin/python -m pytest tests/util/test_boi_bank_handler.py -v
8. If any test fails, fix handler. Repeat until all 12 tests pass.
9. git add -A && git commit -m "feat(bank-ocr): BOI parser v2 (${SUFFIX})"
10. git push -u origin aiforge/ONE-48-${SUFFIX}-boi-v2
11. gh pr create --base master --title "ONE-48 BOI parser v2 (${SUFFIX})" --body "passes 12/12 tests" --assignee @me
12. Print the commit SHA and PR URL on the final line.

You have 300 turns. Use them. Do not stop early.
EOF
}

for entry in "${CANDIDATES[@]}"; do
  IFS='|' read -r MODEL CTX SUFFIX <<< "$entry"
  BRANCH="aiforge/ONE-48-${SUFFIX}-boi-v2"
  echo "============================================================"
  echo " Direct eval: $MODEL ctx=$CTX branch=$BRANCH"
  echo "============================================================"
  START=$(date +%s)

  # 1. Unload + load
  remote "\$HOME/.lmstudio/bin/lms unload --all 2>&1 | tail -2; sleep 3"
  LOAD=$(remote "\$HOME/.lmstudio/bin/lms load -y '$MODEL' -c $CTX --gpu max 2>&1 | tail -2" || true)
  echo "  load: $(echo "$LOAD" | tr '\n' ' ' | tail -c 100)"
  if ! echo "$LOAD" | grep -q "loaded successfully\|To use the model"; then
    echo "  SKIP $MODEL — load failed"
    echo "$MODEL,$CTX,LOAD_FAILED,0,12,0,0,,,0,load_failed" >> "$OUT_CSV"
    continue
  fi

  # 2. Ensure clean repo state, delete old aiforge v2 branch if present
  remote "cd ~/codeRepo/PosPythonBackend && git checkout master 2>&1 | tail -1 && git branch -D '$BRANCH' 2>/dev/null || true"

  # 3. Write prompt to remote
  PROMPT_FILE="/tmp/boi-v2-prompt-${SUFFIX}.txt"
  PROMPT_LOCAL=$(mktemp)
  build_prompt "$SUFFIX" > "$PROMPT_LOCAL"
  scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$PROMPT_LOCAL" "$SSH_HOST:$PROMPT_FILE"
  rm -f "$PROMPT_LOCAL"

  # 4. Run hermes DIRECT — single long session, no paperclip
  HERMES_LOG="/tmp/hermes-boi-v2-${SUFFIX}.log"
  echo "  launching hermes (max-turns=$TURNS, timeout=${SECS}s)..."
  remote "cd ~/codeRepo/PosPythonBackend && /opt/homebrew/bin/gtimeout ${SECS}s ~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_FILE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?" || true
  END=$(date +%s); WALL=$((END - START))

  # 5. Collect results
  BRANCH_FOUND=$(remote "cd ~/codeRepo/PosPythonBackend && git branch -a 2>/dev/null | grep -o '$BRANCH' | head -1" || true)
  FILE=$(remote "cd ~/codeRepo/PosPythonBackend && git show '$BRANCH:app/util/boi_bank_handler.py' 2>/dev/null | wc -l | tr -d ' '" || echo 0)
  [[ -z "$FILE" || "$FILE" == "0" ]] && FILE=0 || FILE=1
  TESTFILE=$(remote "cd ~/codeRepo/PosPythonBackend && git show '$BRANCH:tests/util/test_boi_bank_handler.py' 2>/dev/null | grep -c 'T1\|T11\|T2b' || true")
  [[ -z "$TESTFILE" || "$TESTFILE" == "0" ]] && TESTFILE=0 || TESTFILE=1
  SHA=$(remote "cd ~/codeRepo/PosPythonBackend && git log -1 --format=%H '$BRANCH' 2>/dev/null" || echo "")
  PR_URL=$(remote "cd ~/codeRepo/PosPythonBackend && gh pr list --head '$BRANCH' --json url --jq '.[0].url' 2>/dev/null" || echo "")

  TESTS_PASSED=0
  FINAL_ST="unknown"
  if [[ -n "$BRANCH_FOUND" && "$FILE" == "1" && "$TESTFILE" == "1" ]]; then
    TEST_OUT=$(remote "cd ~/codeRepo/PosPythonBackend && git checkout '$BRANCH' >/dev/null 2>&1; ./venv/bin/python -m pytest tests/util/test_boi_bank_handler.py --no-header -q 2>&1 | tail -10" || true)
    TESTS_PASSED=$(echo "$TEST_OUT" | grep -oE '[0-9]+ passed' | head -1 | grep -oE '[0-9]+' || echo 0)
    [[ "$TESTS_PASSED" == "12" ]] && FINAL_ST="done" || FINAL_ST="partial"
    echo "  pytest: $(echo "$TEST_OUT" | tail -1)"
  else
    FINAL_ST="incomplete"
  fi

  echo "  RESULT: model=$MODEL branch=$BRANCH_FOUND tests=$TESTS_PASSED/12 file=$FILE test_file=$TESTFILE sha=${SHA:0:8} pr=$PR_URL wall=${WALL}s status=$FINAL_ST"
  echo "$MODEL,$CTX,$BRANCH_FOUND,$TESTS_PASSED,12,$FILE,$TESTFILE,$SHA,$PR_URL,$WALL,$FINAL_ST" >> "$OUT_CSV"
  echo
done

echo "=============== direct eval done ==============="
column -t -s, "$OUT_CSV" 2>/dev/null || cat "$OUT_CSV"
