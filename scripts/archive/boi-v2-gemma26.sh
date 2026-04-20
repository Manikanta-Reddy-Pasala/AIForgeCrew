#!/usr/bin/env bash
# Test gemma-4-26b-a4b-it (26B MoE, 4B active) on ONE-48 BOI v2.
set -uo pipefail

SSH_HOST="${1:-manikanta@192.168.70.185}"
MODEL="gemma-4-26b-a4b-it"
CTX=65536
SUFFIX="gemma26"
BRANCH="aiforge/ONE-48-${SUFFIX}-boi-v2"
OUT_CSV="docs/eval/boi-v2-bench.csv"
TURNS=300

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

echo "============================================================"
echo " Gemma-26B eval: $MODEL ctx=$CTX branch=$BRANCH (NO gtimeout)"
echo "============================================================"
START=$(date +%s)

remote "\$HOME/.lmstudio/bin/lms unload --all 2>&1 | tail -2; sleep 3"
LOAD=$(remote "\$HOME/.lmstudio/bin/lms load -y '$MODEL' -c $CTX --gpu max 2>&1 | tail -3" || true)
echo "  load: $(echo "$LOAD" | tr '\n' ' ' | tail -c 120)"
if ! echo "$LOAD" | grep -q "loaded successfully\|To use the model"; then
  echo "  SKIP — load failed"
  echo "$MODEL,$CTX,LOAD_FAILED,0,13,0,0,,,0,load_failed" >> "$OUT_CSV"
  exit 1
fi

remote "cd ~/codeRepo/PosPythonBackend && git checkout master 2>&1 | tail -1 && git branch -D '$BRANCH' 2>/dev/null || true"

PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<EOF
You are Sr Developer on AIForgeCrew. Execute this ticket end-to-end in ONE session. Do NOT return until all steps are done.

TICKET: ONE-48 — Bank of India OCR parser v2 (gemma-4-26b re-eval)

REPO: /Users/manikanta/codeRepo/PosPythonBackend (current directory)

FILES ALREADY STAGED (read these first):
  - tests/fixtures/BOI.pdf              (sample statement)
  - tests/fixtures/boi_expected.json    (golden output, 12 txns hand-verified)
  - tests/fixtures/ONE-48-test-spec.py  (required test file, 12 tests you MUST copy over verbatim)

TARGET FILES:
  - app/util/boi_bank_handler.py  (replace — keep export parse(pdf_path) AND boi_handler=parse alias)
  - tests/util/test_boi_bank_handler.py (copy from tests/fixtures/ONE-48-test-spec.py)
  - tests/util/conftest.py (add sys.path.insert so app.util.boi_bank_handler imports)

ACCEPTANCE: parse returns {metadata:{...}, transactions:[...]} matching boi_expected.json schema.

NON-NEGOTIABLES:
  - direction inferred from Withdrawal vs Deposits column (not hardcoded)
  - Indian lakh/crore format parsing (1,14,000.00 -> 114000.00)
  - preserve negative balances
  - chequeNo null when blank (not "")
  - dates ISO YYYY-MM-DD
  - description text MUST match golden exactly (watch pdfplumber line-wraps — join lines carefully, don't strip trailing words)

PYTEST: /usr/bin/python3 -m pytest tests/util/test_boi_bank_handler.py --no-header -q

STEPS (complete each before moving on):
1. cd /Users/manikanta/codeRepo/PosPythonBackend
2. git checkout master && git pull origin master
3. git checkout -b aiforge/ONE-48-gemma26-boi-v2
4. cp tests/fixtures/ONE-48-test-spec.py tests/util/test_boi_bank_handler.py
5. Write tests/util/conftest.py: import sys, os; sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
6. Write app/util/boi_bank_handler.py — MUST end with: boi_handler = parse
7. Run pytest. If failing, fix. Repeat until 12/12 (or 13/13) pass.
8. AS SOON AS tests green: git add -A && git commit -m "feat(bank-ocr): BOI parser v2 (gemma-4-26b)"
9. git push -u origin aiforge/ONE-48-gemma26-boi-v2
10. gh pr create --base master --title "ONE-48 BOI parser v2 (gemma-4-26b)" --body "passes tests" --assignee @me
11. Print commit SHA and PR URL.

IMPORTANT: Do not stop editing until pytest reports "N passed, 0 failed". Then IMMEDIATELY commit+push.
You have 300 turns. No wall clock limit.
EOF

PROMPT_REMOTE="/tmp/boi-v2-prompt-gemma26.txt"
scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$PROMPT_LOCAL" "$SSH_HOST:$PROMPT_REMOTE"
rm -f "$PROMPT_LOCAL"

HERMES_LOG="/tmp/hermes-boi-v2-gemma26.log"
echo "  launching gemma-4-26b hermes (max-turns=$TURNS, NO gtimeout)..."
# No gtimeout wrapper — let it run until hermes exits or max-turns hit
remote "cd ~/codeRepo/PosPythonBackend && ~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
END=$(date +%s); WALL=$((END - START))

BRANCH_FOUND=$(remote "cd ~/codeRepo/PosPythonBackend && git branch -a 2>/dev/null | grep -o '$BRANCH' | head -1" || true)
SHA=$(remote "cd ~/codeRepo/PosPythonBackend && git log -1 --format=%H '$BRANCH' 2>/dev/null" || echo "")
PR_URL=$(remote "cd ~/codeRepo/PosPythonBackend && gh pr list --head '$BRANCH' --json url --jq '.[0].url' 2>/dev/null" || echo "")
FILE=$(remote "cd ~/codeRepo/PosPythonBackend && git show '$BRANCH:app/util/boi_bank_handler.py' 2>/dev/null | wc -l | tr -d ' '" || echo 0)
[[ -z "$FILE" || "$FILE" == "0" ]] && FILE=0 || FILE=1
TESTFILE=$(remote "cd ~/codeRepo/PosPythonBackend && git show '$BRANCH:tests/util/test_boi_bank_handler.py' 2>/dev/null | grep -c 'T1\|T11\|T2b' || true")
[[ -z "$TESTFILE" || "$TESTFILE" == "0" ]] && TESTFILE=0 || TESTFILE=1

# If branch has no commits but has uncommitted diff, capture as WIP for parity
if [[ -n "$BRANCH_FOUND" && "$SHA" == "" ]]; then
  remote "cd ~/codeRepo/PosPythonBackend && git checkout '$BRANCH' 2>&1 | tail -1; git add -A app/util/boi_bank_handler.py tests/util/test_boi_bank_handler.py tests/util/conftest.py 2>/dev/null; git commit -m 'WIP gemma-4-26b BOI v2 (exited, incomplete)' 2>&1 | tail -2" || true
  SHA=$(remote "cd ~/codeRepo/PosPythonBackend && git log -1 --format=%H '$BRANCH' 2>/dev/null" || echo "")
  FILE=$(remote "cd ~/codeRepo/PosPythonBackend && git show '$BRANCH:app/util/boi_bank_handler.py' 2>/dev/null | wc -l | tr -d ' '")
  [[ -z "$FILE" || "$FILE" == "0" ]] && FILE=0 || FILE=1
  TESTFILE=$(remote "cd ~/codeRepo/PosPythonBackend && git show '$BRANCH:tests/util/test_boi_bank_handler.py' 2>/dev/null | grep -c 'T1\|T11\|T2b' || true")
  [[ -z "$TESTFILE" || "$TESTFILE" == "0" ]] && TESTFILE=0 || TESTFILE=1
fi

TESTS_PASSED=0
FINAL_ST="incomplete"
if [[ -n "$BRANCH_FOUND" && "$FILE" == "1" ]]; then
  # Apply alias if missing for test collection
  remote "cd ~/codeRepo/PosPythonBackend && git checkout '$BRANCH' >/dev/null 2>&1; grep -q 'boi_handler = parse' app/util/boi_bank_handler.py || printf '\n\nboi_handler = parse\n' >> app/util/boi_bank_handler.py"
  TEST_OUT=$(remote "cd ~/codeRepo/PosPythonBackend && /usr/bin/python3 -m pytest tests/util/test_boi_bank_handler.py --no-header -q 2>&1 | tail -15" || true)
  TESTS_PASSED=$(echo "$TEST_OUT" | grep -oE '[0-9]+ passed' | head -1 | grep -oE '[0-9]+' || echo 0)
  [[ "$TESTS_PASSED" == "12" || "$TESTS_PASSED" == "13" ]] && FINAL_ST="done" || FINAL_ST="partial"
  echo "  pytest: $(echo "$TEST_OUT" | tail -1)"
  # Revert alias if we added it (don't pollute gemma's actual output)
  remote "cd ~/codeRepo/PosPythonBackend && git checkout app/util/boi_bank_handler.py 2>/dev/null" || true
fi

echo "  RESULT: model=$MODEL branch=$BRANCH_FOUND tests=$TESTS_PASSED/13 file=$FILE test_file=$TESTFILE sha=${SHA:0:8} pr=$PR_URL wall=${WALL}s status=$FINAL_ST"
echo "$MODEL,$CTX,$BRANCH_FOUND,$TESTS_PASSED,13,$FILE,$TESTFILE,$SHA,$PR_URL,$WALL,$FINAL_ST" >> "$OUT_CSV"

remote "cd ~/codeRepo/PosPythonBackend && git checkout master 2>&1 | tail -1" || true
echo "done"
