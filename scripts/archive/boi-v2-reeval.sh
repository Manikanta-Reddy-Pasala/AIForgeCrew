#!/usr/bin/env bash
# scripts/boi-v2-reeval.sh — ONE-48 BOI parser v2 re-evaluation.
# Runs two candidate models sequentially against the same BOI ticket with
# T1-T11 acceptance tests pre-staged in tests/fixtures/.
#
# Usage: bash scripts/boi-v2-reeval.sh [SSH_HOST]
set -euo pipefail

declare -a CANDIDATES=(
  "qwen3-coder-next|65536|Qwen3-Coder-Next-80B-A3B"
  "gemma-4-31b-it|32768|Gemma-4-31B-dense"
)

SSH_HOST="${1:-manikanta@192.168.70.185}"
CID="${CID:-fd294bd0-2f65-405f-b443-fb41d66226fb}"
SRDEV="${SRDEV:-28b8c064-bfcf-44e1-9e91-e37c39e0097c}"
WAIT_MINUTES="${WAIT_MINUTES:-50}"
OUT_CSV="${OUT_CSV:-docs/eval/boi-v2-bench.csv}"

mkdir -p "$(dirname "$OUT_CSV")"
[[ -f "$OUT_CSV" ]] || echo "model,ctx,ticket,tests_passed,tests_total,file_created,test_file_created,branch,commit_sha,pr_url,wall_seconds,input_tokens,output_tokens,final_status" > "$OUT_CSV"

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }
rpsql()  { remote "PGPASSWORD=paperclip \$HOME/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"$1\""; }

TICKET_DESC='ONE-48 — BOI bank OCR parser v2 (re-evaluation).

Target file: ~/codeRepo/PosPythonBackend/app/util/boi_bank_handler.py
Test file:   ~/codeRepo/PosPythonBackend/tests/util/test_boi_bank_handler.py

Acceptance spec already staged on disk — READ THESE FIRST:
  - ~/codeRepo/PosPythonBackend/tests/fixtures/BOI.pdf          (sample statement)
  - ~/codeRepo/PosPythonBackend/tests/fixtures/boi_expected.json (golden output)
  - ~/codeRepo/PosPythonBackend/tests/fixtures/ONE-48-test-spec.py (required test file, 12 tests)

Steps:
1. Copy tests/fixtures/ONE-48-test-spec.py to tests/util/test_boi_bank_handler.py and add a conftest.py or pytest ini to make the app.util.boi_bank_handler import resolve.
2. Replace app/util/boi_bank_handler.py with a new implementation that exposes parse(pdf_path: str) -> dict matching the schema in boi_expected.json (metadata wrapper + transactions with serial, txn_date ISO, description, chequeNo nullable, transactionAmount, transactionDirection CR|DR, balance).
3. Run: ./venv/bin/python -m pytest tests/util/test_boi_bank_handler.py -v
4. All 12 tests must pass.
5. git checkout -b aiforge/ONE-48-<MODEL_SUFFIX>-boi-v2
6. git add + commit + push + gh pr create --base master
7. Post commit SHA and PR URL as a ticket comment BEFORE marking done.

Non-negotiables:
- direction MUST be inferred from Withdrawal/Deposits columns (not hardcoded)
- amounts MUST handle Indian lakh/crore format (1,14,000.00 -> 114000.00)
- negative balances MUST be preserved (Cash Credit accounts)
- chequeNo MUST be null when blank, NOT empty string
- dates MUST be ISO (YYYY-MM-DD) not DD-MM-YYYY
- metadata MUST include bank_name, branch, account_name, account_number, customer_id, account_type, ifsc_code, statement_period_start, statement_period_end, statement_generated_at

If you claim done without running the tests and getting 12/12 green, Paperclip will verify and mark you as having lied.'

for i in "${!CANDIDATES[@]}"; do
  IFS='|' read -r MODEL CTX FRIENDLY <<< "${CANDIDATES[$i]}"
  case "$MODEL" in
    qwen3-coder-next) SUFFIX="qwen" ;;
    gemma-4-31b-it)   SUFFIX="gemma" ;;
    *)                SUFFIX="$MODEL" ;;
  esac

  echo "======================================================================"
  echo " [$((i+1))/${#CANDIDATES[@]}]  $FRIENDLY  @ ctx=$CTX  branch-suffix=$SUFFIX"
  echo "======================================================================"

  # 1. Unload all + kill hermes
  remote "\$HOME/.lmstudio/bin/lms unload --all 2>&1 | tail -3; pkill -f 'hermes chat' 2>/dev/null || true; sleep 3"

  # 2. Load candidate
  LOAD_OUT=$(remote "\$HOME/.lmstudio/bin/lms load -y '$MODEL' -c $CTX --gpu max 2>&1 | tail -3" || true)
  echo "  load: $LOAD_OUT"
  if ! echo "$LOAD_OUT" | grep -q "loaded successfully\|To use the model"; then
    echo "  SKIP — load failed"
    echo "$MODEL,$CTX,SKIPPED,0,12,0,0,,,,0,0,0,load_failed" >> "$OUT_CSV"
    continue
  fi

  # 3. Refresh Hermes ctx cache
  remote "python3 - <<PY
import re
from pathlib import Path
p = Path.home() / '.hermes' / 'context_length_cache.yaml'
txt = p.read_text() if p.exists() else 'context_lengths:\n'
pattern = re.compile(r'^(\s+)$MODEL@http://localhost:1234/v1:\s+\d+', re.MULTILINE)
if pattern.search(txt):
    txt = pattern.sub(lambda m: f'{m.group(1)}$MODEL@http://localhost:1234/v1: $CTX', txt)
else:
    txt = txt.rstrip() + '\n  $MODEL@http://localhost:1234/v1: $CTX\n'
p.write_text(txt)
print('ctx cache updated')
PY
"

  # 4. Swap Sr Dev adapter model + clear session state
  rpsql "UPDATE agents SET adapter_config = jsonb_set(adapter_config, '{model}', '\\\"$MODEL\\\"'::jsonb) WHERE id = '$SRDEV'" >/dev/null
  rpsql "UPDATE agent_runtime_state SET session_id = NULL, last_error = NULL WHERE agent_id = '$SRDEV'" >/dev/null
  rpsql "UPDATE agent_task_sessions SET session_display_id = NULL, session_params_json = '{}'::jsonb WHERE agent_id = '$SRDEV'" >/dev/null

  # 5. Restart Paperclip
  remote "launchctl kickstart -k gui/\$(id -u)/com.aiforge.paperclip 2>/dev/null; for _ in \$(seq 1 10); do curl -sf http://localhost:3100/api/health >/dev/null 2>&1 && break; sleep 2; done"

  # 6. Create ticket — write DESC to local file, scp to remote, build JSON there
  START=$(date +%s)
  TITLE="ONE-48 BOI parser v2 ($SUFFIX re-eval)"
  DESC_LOCAL=$(mktemp)
  # shellcheck disable=SC2001
  echo "${TICKET_DESC//<MODEL_SUFFIX>/$SUFFIX}" > "$DESC_LOCAL"
  DESC_REMOTE="/tmp/boi-v2-desc-$$-$i.txt"
  PAYLOAD_REMOTE="/tmp/boi-v2-payload-$$-$i.json"
  scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$DESC_LOCAL" "$SSH_HOST:$DESC_REMOTE"
  rm -f "$DESC_LOCAL"

  remote "~/.hermes/hermes-agent/venv/bin/python3 - <<'PYEOF' > $PAYLOAD_REMOTE
import json
desc = open('$DESC_REMOTE').read()
print(json.dumps({
  'title': '$TITLE',
  'description': desc,
  'priority': 'high',
  'status': 'todo',
  'assigneeAgentId': '$SRDEV',
}))
PYEOF"

  TICKET=$(remote "curl -s -X POST 'http://localhost:3100/api/companies/$CID/issues' -H 'Content-Type: application/json' --data @$PAYLOAD_REMOTE | ~/.hermes/hermes-agent/venv/bin/python3 -c 'import sys,json; d=json.loads(sys.stdin.read()); print(d.get(\"identifier\",\"?\"))'")
  echo "  ticket: $TICKET"

  # 7. Wait for terminal state
  DEADLINE=$((START + WAIT_MINUTES * 60))
  FINAL_ST=""
  while (( $(date +%s) < DEADLINE )); do
    FINAL_ST=$(rpsql "SELECT status FROM issues WHERE identifier = '$TICKET'")
    echo "    $(date +%H:%M:%S) $TICKET status=$FINAL_ST"
    [[ "$FINAL_ST" == "done" || "$FINAL_ST" == "cancelled" ]] && break
    sleep 60
  done
  END=$(date +%s)
  WALL=$((END - START))

  # 8. Run tests on agent's branch + collect metrics
  BRANCH=$(remote "cd ~/codeRepo/PosPythonBackend && git branch -a 2>/dev/null | grep -o 'aiforge/ONE-48-${SUFFIX}-boi-v2' | head -1" || true)
  FILE=$(remote "test -f ~/codeRepo/PosPythonBackend/app/util/boi_bank_handler.py && echo 1 || echo 0")
  TESTFILE=$(remote "test -f ~/codeRepo/PosPythonBackend/tests/util/test_boi_bank_handler.py && echo 1 || echo 0")
  SHA=$(remote "cd ~/codeRepo/PosPythonBackend && git log --all --grep='$TICKET' -1 --format=%H 2>/dev/null" || true)
  PR_URL=$(rpsql "SELECT url FROM issue_work_products WHERE issue_id = (SELECT id FROM issues WHERE identifier = '$TICKET') AND type = 'pull_request' LIMIT 1" || true)
  TOK_IN=$(rpsql "SELECT COALESCE(SUM((usage_json->>'input_tokens')::int),0) FROM heartbeat_runs WHERE context_snapshot::text LIKE '%$TICKET%'" || echo 0)
  TOK_OUT=$(rpsql "SELECT COALESCE(SUM((usage_json->>'output_tokens')::int),0) FROM heartbeat_runs WHERE context_snapshot::text LIKE '%$TICKET%'" || echo 0)

  TESTS_PASSED=0
  if [[ -n "$BRANCH" && "$FILE" == "1" && "$TESTFILE" == "1" ]]; then
    TEST_OUT=$(remote "cd ~/codeRepo/PosPythonBackend && git checkout '$BRANCH' >/dev/null 2>&1; ./venv/bin/python -m pytest tests/util/test_boi_bank_handler.py --no-header -q 2>&1 | tail -10" || true)
    TESTS_PASSED=$(echo "$TEST_OUT" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' | head -1 || echo 0)
    echo "  pytest: $TEST_OUT"
  fi

  echo "  RESULT: status=$FINAL_ST tests=$TESTS_PASSED/12 file=$FILE test=$TESTFILE sha=$SHA pr=$PR_URL wall=${WALL}s tok=${TOK_IN}/${TOK_OUT}"
  echo "$MODEL,$CTX,$TICKET,$TESTS_PASSED,12,$FILE,$TESTFILE,$BRANCH,$SHA,$PR_URL,$WALL,$TOK_IN,$TOK_OUT,$FINAL_ST" >> "$OUT_CSV"

  # 9. Cancel ticket + cleanup for next run
  rpsql "UPDATE issues SET status = 'cancelled' WHERE identifier = '$TICKET'" >/dev/null
  remote "pkill -f 'hermes chat' 2>/dev/null || true"
  echo
done

echo
echo "=============== boi v2 bench done ==============="
column -t -s, "$OUT_CSV"
