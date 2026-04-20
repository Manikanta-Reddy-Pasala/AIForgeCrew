#!/usr/bin/env bash
# scripts/boi-v2-watch.sh — watch ONE-48 (qwen, already created), then dispatch
# ONE-49 (gemma-4-31b-it) using same spec, then report.
#
# Assumes: qwen3-coder-next already loaded in LM Studio, ONE-48 exists + assigned
# to Sr Dev, DESC already on remote at /tmp/boi-v2-desc-*.txt.
set -uo pipefail

SSH_HOST="${1:-manikanta@192.168.70.185}"
CID="${CID:-fd294bd0-2f65-405f-b443-fb41d66226fb}"
SRDEV="${SRDEV:-28b8c064-bfcf-44e1-9e91-e37c39e0097c}"
WAIT_MINUTES="${WAIT_MINUTES:-50}"
OUT_CSV="${OUT_CSV:-docs/eval/boi-v2-bench.csv}"

mkdir -p "$(dirname "$OUT_CSV")"
[[ -f "$OUT_CSV" ]] || echo "model,ctx,ticket,tests_passed,tests_total,file_created,test_file_created,branch,commit_sha,pr_url,wall_seconds,input_tokens,output_tokens,final_status" > "$OUT_CSV"

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }
rpsql()  { remote "PGPASSWORD=paperclip \$HOME/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"$1\""; }

collect_and_log() {
  local MODEL="$1" CTX="$2" TICKET="$3" SUFFIX="$4" START="$5" FINAL_ST="$6"
  local END WALL BRANCH FILE TESTFILE SHA PR_URL TOK_IN TOK_OUT TEST_OUT TESTS_PASSED
  END=$(date +%s); WALL=$((END - START))
  BRANCH=$(remote "cd ~/codeRepo/PosPythonBackend && git branch -a 2>/dev/null | grep -o 'aiforge/ONE-${TICKET#ONE-}-${SUFFIX}-boi-v2' | head -1" || true)
  FILE=$(remote "test -f ~/codeRepo/PosPythonBackend/app/util/boi_bank_handler.py && echo 1 || echo 0")
  TESTFILE=$(remote "test -f ~/codeRepo/PosPythonBackend/tests/util/test_boi_bank_handler.py && echo 1 || echo 0")
  SHA=$(remote "cd ~/codeRepo/PosPythonBackend && git log --all --grep='$TICKET' -1 --format=%H 2>/dev/null" || true)
  PR_URL=$(rpsql "SELECT url FROM issue_work_products WHERE issue_id = (SELECT id FROM issues WHERE identifier = '$TICKET') AND type = 'pull_request' LIMIT 1" 2>/dev/null || echo "")
  TOK_IN=$(rpsql "SELECT COALESCE(SUM((usage_json->>'input_tokens')::int),0) FROM heartbeat_runs WHERE context_snapshot::text LIKE '%$TICKET%'" 2>/dev/null || echo 0)
  TOK_OUT=$(rpsql "SELECT COALESCE(SUM((usage_json->>'output_tokens')::int),0) FROM heartbeat_runs WHERE context_snapshot::text LIKE '%$TICKET%'" 2>/dev/null || echo 0)
  TESTS_PASSED=0
  if [[ -n "$BRANCH" && "$FILE" == "1" && "$TESTFILE" == "1" ]]; then
    TEST_OUT=$(remote "cd ~/codeRepo/PosPythonBackend && git checkout '$BRANCH' >/dev/null 2>&1; ./venv/bin/python -m pytest tests/util/test_boi_bank_handler.py --no-header -q 2>&1 | tail -10" || true)
    TESTS_PASSED=$(echo "$TEST_OUT" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' | head -1 || echo 0)
    echo "  pytest: $TEST_OUT"
  fi
  echo "  RESULT: model=$MODEL ticket=$TICKET status=$FINAL_ST tests=$TESTS_PASSED/12 file=$FILE test=$TESTFILE sha=$SHA pr=$PR_URL wall=${WALL}s tok=${TOK_IN}/${TOK_OUT}"
  echo "$MODEL,$CTX,$TICKET,$TESTS_PASSED,12,$FILE,$TESTFILE,$BRANCH,$SHA,$PR_URL,$WALL,$TOK_IN,$TOK_OUT,$FINAL_ST" >> "$OUT_CSV"
}

poll_until_terminal() {
  local TICKET="$1" START="$2"
  local DEADLINE=$((START + WAIT_MINUTES * 60))
  local st=""
  while (( $(date +%s) < DEADLINE )); do
    st=$(rpsql "SELECT status FROM issues WHERE identifier = '$TICKET'")
    echo "    $(date +%H:%M:%S) $TICKET status=$st"
    [[ "$st" == "done" || "$st" == "cancelled" ]] && break
    sleep 60
  done
  echo "$st"
}

echo "============ Run A: qwen3-coder-next on ONE-48 ============"
START_A=$(date +%s)
ST_A=$(poll_until_terminal "ONE-48" "$START_A")
collect_and_log "qwen3-coder-next" 65536 "ONE-48" "qwen" "$START_A" "$ST_A"
rpsql "UPDATE issues SET status = 'cancelled' WHERE identifier = 'ONE-48' AND status NOT IN ('done','cancelled')" >/dev/null
remote "pkill -f 'hermes chat' 2>/dev/null || true"

echo
echo "============ Run B: swap to gemma-4-31b-it ============"
# Swap model
remote "\$HOME/.lmstudio/bin/lms unload --all 2>&1 | tail -3; sleep 3"
LOAD_OUT=$(remote "\$HOME/.lmstudio/bin/lms load -y gemma-4-31b-it -c 32768 --gpu max 2>&1 | tail -3" || true)
echo "  load: $(echo "$LOAD_OUT" | tail -2)"
if ! echo "$LOAD_OUT" | grep -q "loaded successfully\|To use the model"; then
  echo "  SKIP — gemma load failed"
  echo "gemma-4-31b-it,32768,SKIPPED,0,12,0,0,,,,0,0,0,load_failed" >> "$OUT_CSV"
  exit 0
fi

# Hermes ctx cache
remote "python3 - <<'PYEOF'
import re
from pathlib import Path
p = Path.home() / '.hermes' / 'context_length_cache.yaml'
txt = p.read_text() if p.exists() else 'context_lengths:\n'
pattern = re.compile(r'^(\s+)gemma-4-31b-it@http://localhost:1234/v1:\s+\d+', re.MULTILINE)
if pattern.search(txt):
    txt = pattern.sub(lambda m: f'{m.group(1)}gemma-4-31b-it@http://localhost:1234/v1: 32768', txt)
else:
    txt = txt.rstrip() + '\n  gemma-4-31b-it@http://localhost:1234/v1: 32768\n'
p.write_text(txt)
print('ctx cache updated')
PYEOF
"

# Swap adapter + clear state
rpsql "UPDATE agents SET adapter_config = jsonb_set(adapter_config, '{model}', '\\\"gemma-4-31b-it\\\"'::jsonb) WHERE id = '$SRDEV'" >/dev/null
rpsql "UPDATE agent_runtime_state SET session_id = NULL, last_error = NULL WHERE agent_id = '$SRDEV'" >/dev/null
rpsql "UPDATE agent_task_sessions SET session_display_id = NULL, session_params_json = '{}'::jsonb WHERE agent_id = '$SRDEV'" >/dev/null

# Restart Paperclip
remote "launchctl kickstart -k gui/\$(id -u)/com.aiforge.paperclip 2>/dev/null"
for _ in $(seq 1 30); do
  if remote "curl -sf http://localhost:3100/api/health >/dev/null 2>&1"; then
    break
  fi
  sleep 2
done
sleep 3

# Create ONE-49 using already-staged DESC file with suffix swap
DESC_FILE_REMOTE=$(remote "ls -t /tmp/boi-v2-desc-*.txt | head -1")
echo "  using desc: $DESC_FILE_REMOTE"

# Rewrite desc with gemma suffix
remote "sed 's/ONE-48-qwen-boi-v2/ONE-48-gemma-boi-v2/g' '$DESC_FILE_REMOTE' > /tmp/boi-v2-desc-gemma.txt"
PAYLOAD_REMOTE="/tmp/boi-v2-payload-gemma.json"
remote "~/.hermes/hermes-agent/venv/bin/python3 - <<'PYEOF' > $PAYLOAD_REMOTE
import json
desc = open('/tmp/boi-v2-desc-gemma.txt').read()
print(json.dumps({
  'title': 'ONE-49 BOI parser v2 (gemma re-eval)',
  'description': desc,
  'priority': 'high',
  'status': 'todo',
  'assigneeAgentId': '$SRDEV',
}))
PYEOF"

RESP=$(remote "curl -s -X POST 'http://localhost:3100/api/companies/$CID/issues' -H 'Content-Type: application/json' --data @$PAYLOAD_REMOTE")
echo "  resp head: $(echo "$RESP" | head -c 200)"
TICKET_B=$(echo "$RESP" | ~/.hermes/hermes-agent/venv/bin/python3 -c 'import sys,json; d=json.loads(sys.stdin.read()); print(d.get("identifier","?"))' 2>/dev/null || echo "?")
# fallback: local python
[[ "$TICKET_B" == "?" || -z "$TICKET_B" ]] && TICKET_B=$(echo "$RESP" | python3 -c 'import sys,json; d=json.loads(sys.stdin.read()); print(d.get("identifier","?"))' 2>/dev/null || echo "?")
echo "  ticket B: $TICKET_B"

if [[ "$TICKET_B" == "?" || -z "$TICKET_B" ]]; then
  echo "  SKIP Run B — ticket creation failed"
  echo "gemma-4-31b-it,32768,CREATE_FAILED,0,12,0,0,,,,0,0,0,create_failed" >> "$OUT_CSV"
  exit 0
fi

START_B=$(date +%s)
ST_B=$(poll_until_terminal "$TICKET_B" "$START_B")
collect_and_log "gemma-4-31b-it" 32768 "$TICKET_B" "gemma" "$START_B" "$ST_B"
rpsql "UPDATE issues SET status = 'cancelled' WHERE identifier = '$TICKET_B' AND status NOT IN ('done','cancelled')" >/dev/null
remote "pkill -f 'hermes chat' 2>/dev/null || true"

echo
echo "=============== boi v2 bench done ==============="
column -t -s, "$OUT_CSV"
