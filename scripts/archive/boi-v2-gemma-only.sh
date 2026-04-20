#!/usr/bin/env bash
# Gemma-only rerun with ctx=131072 (Hermes requires ≥64K).
set -uo pipefail

SSH_HOST="${1:-manikanta@192.168.70.185}"
MODEL="gemma-4-31b-it"
CTX=131072
SUFFIX="gemma"
BRANCH="aiforge/ONE-48-${SUFFIX}-boi-v2"
OUT_CSV="docs/eval/boi-v2-bench.csv"
TURNS=300
SECS=3600

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

echo "============================================================"
echo " Gemma eval: $MODEL ctx=$CTX branch=$BRANCH"
echo "============================================================"
START=$(date +%s)

remote "\$HOME/.lmstudio/bin/lms unload --all 2>&1 | tail -2; sleep 3"
LOAD=$(remote "\$HOME/.lmstudio/bin/lms load -y '$MODEL' -c $CTX --gpu max 2>&1 | tail -3" || true)
echo "  load: $(echo "$LOAD" | tr '\n' ' ' | tail -c 120)"
if ! echo "$LOAD" | grep -q "loaded successfully\|To use the model"; then
  echo "  SKIP — load failed"
  echo "$MODEL,$CTX,LOAD_FAILED,0,12,0,0,,,0,load_failed" >> "$OUT_CSV"
  exit 1
fi

remote "cd ~/codeRepo/PosPythonBackend && git checkout master 2>&1 | tail -1 && git branch -D '$BRANCH' 2>/dev/null || true"

# Push prompt
PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<EOF
You are Sr Developer on AIForgeCrew. Execute this ticket end-to-end in ONE session. Do NOT return until all steps are done.

TICKET: ONE-48 — Bank of India OCR parser v2

REPO: /Users/manikanta/codeRepo/PosPythonBackend (current directory)

FILES ALREADY STAGED (read these first):
  - tests/fixtures/BOI.pdf              (sample statement)
  - tests/fixtures/boi_expected.json    (golden output, 12 txns hand-verified)
  - tests/fixtures/ONE-48-test-spec.py  (required test file, 12 tests you MUST copy over verbatim)

TARGET FILES TO CREATE/REPLACE:
  - app/util/boi_bank_handler.py  (replace — keep export name parse(pdf_path) AND keep boi_handler alias so existing imports in app/routes/pythonBankOCR.py still work)
  - tests/util/test_boi_bank_handler.py (copy from tests/fixtures/ONE-48-test-spec.py)
  - tests/util/conftest.py (add sys.path.insert to make app.util.boi_bank_handler importable)

ACCEPTANCE: handler exposes parse(pdf_path: str) -> dict AND provides boi_handler = parse alias. parse returns:
  metadata: {bank_name, branch, account_name, account_number, customer_id, account_type, ifsc_code, statement_period_start, statement_period_end, statement_generated_at}
  transactions: [{serial, txn_date (ISO YYYY-MM-DD), description, chequeNo (nullable), transactionAmount, transactionDirection ("CR"|"DR"), balance}, ...]

NON-NEGOTIABLES:
  - direction inferred from Withdrawal vs Deposits column (not hardcoded)
  - Indian lakh/crore format parsing (1,14,000.00 -> 114000.00)
  - preserve negative balances (Cash Credit)
  - chequeNo = null when blank, not empty string
  - dates ISO YYYY-MM-DD, not DD-MM-YYYY

PYTEST COMMAND: /usr/bin/python3 -m pytest tests/util/test_boi_bank_handler.py --no-header -q
(venv is broken, use system python3)

EXECUTION STEPS (do each in order, do not stop until done):
1. cd /Users/manikanta/codeRepo/PosPythonBackend
2. git checkout master && git pull origin master
3. git checkout -b aiforge/ONE-48-gemma-boi-v2
4. cp tests/fixtures/ONE-48-test-spec.py tests/util/test_boi_bank_handler.py
5. Write tests/util/conftest.py with: import sys, os; sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
6. Write the new handler at app/util/boi_bank_handler.py. MUST include: def parse(pdf_path): ... and at end of file: boi_handler = parse
7. Run: /usr/bin/python3 -m pytest tests/util/test_boi_bank_handler.py --no-header -q
8. If any test fails, fix handler. Repeat until 12/12 pass.
9. git add -A && git commit -m "feat(bank-ocr): BOI parser v2 (gemma)"
10. git push -u origin aiforge/ONE-48-gemma-boi-v2
11. gh pr create --base master --title "ONE-48 BOI parser v2 (gemma)" --body "passes 12/12 tests" --assignee @me
12. Print commit SHA and PR URL on final line.

Commit AS SOON as tests pass. Do not keep iterating after green.
You have 300 turns. Use them.
EOF

PROMPT_REMOTE="/tmp/boi-v2-prompt-gemma.txt"
scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$PROMPT_LOCAL" "$SSH_HOST:$PROMPT_REMOTE"
rm -f "$PROMPT_LOCAL"

HERMES_LOG="/tmp/hermes-boi-v2-gemma.log"
echo "  launching gemma hermes (max-turns=$TURNS, timeout=${SECS}s)..."
remote "cd ~/codeRepo/PosPythonBackend && /opt/homebrew/bin/gtimeout ${SECS}s ~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
END=$(date +%s); WALL=$((END - START))

# Collect
BRANCH_FOUND=$(remote "cd ~/codeRepo/PosPythonBackend && git branch -a 2>/dev/null | grep -o '$BRANCH' | head -1" || true)
SHA=$(remote "cd ~/codeRepo/PosPythonBackend && git log -1 --format=%H '$BRANCH' 2>/dev/null" || echo "")
PR_URL=$(remote "cd ~/codeRepo/PosPythonBackend && gh pr list --head '$BRANCH' --json url --jq '.[0].url' 2>/dev/null" || echo "")

FILE=$(remote "cd ~/codeRepo/PosPythonBackend && git show '$BRANCH:app/util/boi_bank_handler.py' 2>/dev/null | wc -l | tr -d ' '" || echo 0)
[[ -z "$FILE" || "$FILE" == "0" ]] && FILE=0 || FILE=1
TESTFILE=$(remote "cd ~/codeRepo/PosPythonBackend && git show '$BRANCH:tests/util/test_boi_bank_handler.py' 2>/dev/null | grep -c 'T1\|T11\|T2b' || true")
[[ -z "$TESTFILE" || "$TESTFILE" == "0" ]] && TESTFILE=0 || TESTFILE=1

TESTS_PASSED=0
FINAL_ST="incomplete"
if [[ -n "$BRANCH_FOUND" && "$FILE" == "1" && "$TESTFILE" == "1" ]]; then
  TEST_OUT=$(remote "cd ~/codeRepo/PosPythonBackend && git checkout '$BRANCH' >/dev/null 2>&1; /usr/bin/python3 -m pytest tests/util/test_boi_bank_handler.py --no-header -q 2>&1 | tail -10" || true)
  TESTS_PASSED=$(echo "$TEST_OUT" | grep -oE '[0-9]+ passed' | head -1 | grep -oE '[0-9]+' || echo 0)
  [[ "$TESTS_PASSED" == "13" || "$TESTS_PASSED" == "12" ]] && FINAL_ST="done" || FINAL_ST="partial"
  echo "  pytest: $(echo "$TEST_OUT" | tail -1)"
fi

echo "  RESULT: model=$MODEL branch=$BRANCH_FOUND tests=$TESTS_PASSED/13 file=$FILE test_file=$TESTFILE sha=${SHA:0:8} pr=$PR_URL wall=${WALL}s status=$FINAL_ST"
echo "$MODEL,$CTX,$BRANCH_FOUND,$TESTS_PASSED,13,$FILE,$TESTFILE,$SHA,$PR_URL,$WALL,$FINAL_ST" >> "$OUT_CSV"

remote "cd ~/codeRepo/PosPythonBackend && git checkout master 2>&1 | tail -1" || true
echo "done"
