#!/usr/bin/env bash
# scripts/benchmark-agent-models.sh — evaluate multiple MLX models against the
# same bank-OCR task + DESIGN §4 pipeline.
#
# For each model:
#   1. Swap Sr Developer's adapter_config.model to the candidate
#   2. Create a fresh ticket (copy of standard AXIS-bank-OCR template)
#   3. Wait for pipeline to run (or timeout)
#   4. Record metrics:
#        - file_created:  app/util/axis_bank_handler.py exists?
#        - test_created:  tests/util/test_axis_bank_handler.py exists?
#        - branch_exists: git branch | grep aiforge/ONE-N…
#        - commit_sha:    git log hash of matching commit
#        - pr_opened:     issue_work_products row with type=pull_request
#        - hallucinated:  ticket status=done but file_created=false
#        - wall_seconds:  time between ticket create → terminal status
#        - tokens:        sum of (input + output) from heartbeat_runs.usage_json
#
# Writes docs/eval/bank-ocr-bench.csv.
#
# Candidate models must be pre-downloaded + loaded in LM Studio. See
# CANDIDATE_MODELS below — edit to add/remove.
#
# Usage:
#   bash scripts/benchmark-agent-models.sh [SSH_HOST]
set -euo pipefail

# Candidate models to evaluate (must be loaded in LM Studio).
CANDIDATE_MODELS=(
  "qwen3.6-35b-a3b"              # current Sr Dev baseline
  "qwen3.5-9b-mlx"               # smallest
  "gemma-4-26b-a4b-it"           # Gemma MoE
  "qwen3-coder-next-4bit"        # 80B A3B — SOTA coder (if downloaded)
  "deepseek-coder-v2-lite-instruct-4bit-mlx"  # MoE A2.4B (if downloaded)
)

# One ticket per candidate. Bank names chosen so no prior handler exists.
# Cycle: PNB, CANARA, BOB (Bank of Baroda), CITI, INDUSIND.
# If #models > #banks, recycle.
CANDIDATE_BANKS=(PNB CANARA BOB CITI INDUSIND)

SSH_HOST="${1:-manikanta@192.168.70.185}"
CID="${CID:-fd294bd0-2f65-405f-b443-fb41d66226fb}"
SRDEV_ID="${SRDEV_ID:-28b8c064-bfcf-44e1-9e91-e37c39e0097c}"
EM_ID="${EM_ID:-35760e2f-4cef-4013-9aff-d93592b5f71e}"
WAIT_MINUTES="${WAIT_MINUTES:-30}"   # per-model wall-clock timeout
OUT_CSV="${OUT_CSV:-docs/eval/bank-ocr-bench.csv}"

mkdir -p "$(dirname "$OUT_CSV")"
[[ -f "$OUT_CSV" ]] || echo "model,bank,ticket,file_created,test_created,branch,commit_sha,pr_url,hallucinated,wall_seconds,input_tokens,output_tokens" > "$OUT_CSV"

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }
rpsql() { remote "PGPASSWORD=paperclip \$HOME/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"$1\""; }

for i in "${!CANDIDATE_MODELS[@]}"; do
  MODEL="${CANDIDATE_MODELS[$i]}"
  BANK="${CANDIDATE_BANKS[$((i % ${#CANDIDATE_BANKS[@]}))]}"

  echo "=============================================================="
  echo " [$((i+1))/${#CANDIDATE_MODELS[@]}] model=$MODEL  bank=$BANK"
  echo "=============================================================="

  # Skip if model not loaded.
  LOADED=$(remote "curl -s http://localhost:1234/api/v0/models | grep -c '\"id\": \"$MODEL\"' || true")
  if [[ "$LOADED" == "0" ]]; then
    echo "  SKIP — model not loaded in LM Studio. Run: lms load -y '$MODEL' -c 65536"
    continue
  fi

  # 1. Swap Sr Dev's model
  rpsql "UPDATE agents SET adapter_config = jsonb_set(adapter_config, '{model}', '\\\"$MODEL\\\"'::jsonb) WHERE id = '$SRDEV_ID'" >/dev/null
  # Kill stale sessions so fresh model takes
  remote "pkill -f 'hermes chat' 2>/dev/null || true; sleep 2"
  rpsql "UPDATE agent_runtime_state SET session_id = NULL WHERE agent_id = '$SRDEV_ID'" >/dev/null
  # Restart Paperclip to reload adapter_config
  remote "launchctl kickstart -k gui/\$(id -u)/com.aiforge.paperclip" 2>/dev/null || true
  remote "for _ in \$(seq 1 15); do curl -sf http://localhost:3100/api/health >/dev/null 2>&1 && break; sleep 2; done"

  # 2. Create ticket assigned to EM
  START=$(date +%s)
  DESC="Add ${BANK} bank OCR parser to PosPythonBackend. Target: app/util/${BANK,,}_bank_handler.py + tests/util/test_${BANK,,}_bank_handler.py. Follow DESIGN pipeline: Tester first (failing pytest), Sr Dev implements, Sr Arch reviews + opens GitHub PR via gh CLI."
  TITLE="Add ${BANK} bank OCR parser"
  PAYLOAD=$(remote "~/.hermes/hermes-agent/venv/bin/python3 -c '
import json
print(json.dumps({
  \"title\": \"$TITLE\",
  \"description\": \"$DESC\",
  \"priority\": \"medium\",
  \"status\": \"backlog\",
  \"assigneeAgentId\": \"$EM_ID\",
}))'")
  TICKET=$(remote "curl -s -X POST 'http://localhost:3100/api/companies/$CID/issues' -H 'Content-Type: application/json' -d '$PAYLOAD' | ~/.hermes/hermes-agent/venv/bin/python3 -c 'import sys,json; d=json.loads(sys.stdin.read()); print(d.get(\"identifier\",\"?\"))'")
  echo "  created ticket: $TICKET"

  # 3. Wait for terminal state (done, cancelled, timed out)
  DEADLINE=$((START + WAIT_MINUTES * 60))
  while (( $(date +%s) < DEADLINE )); do
    ST=$(rpsql "SELECT status FROM issues WHERE identifier = '$TICKET'")
    echo "    $(date +%H:%M:%S) $TICKET status=$ST"
    [[ "$ST" == "done" || "$ST" == "cancelled" ]] && break
    sleep 60
  done
  END=$(date +%s)
  WALL=$((END - START))

  # 4. Collect metrics
  FILE_CREATED=$(remote "test -f ~/codeRepo/PosPythonBackend/app/util/${BANK,,}_bank_handler.py && echo 1 || echo 0")
  TEST_CREATED=$(remote "test -f ~/codeRepo/PosPythonBackend/tests/util/test_${BANK,,}_bank_handler.py && echo 1 || echo 0")
  BRANCH=$(remote "cd ~/codeRepo/PosPythonBackend && git branch --list 'aiforge/*${BANK,,}*' | head -1 | tr -d ' *'" || true)
  COMMIT_SHA=$(remote "cd ~/codeRepo/PosPythonBackend && git log --all --oneline --grep='$TICKET' -1 | awk '{print \$1}'" || true)
  PR_URL=$(rpsql "SELECT url FROM issue_work_products WHERE issue_id = (SELECT id FROM issues WHERE identifier = '$TICKET') AND type = 'pull_request' LIMIT 1" || echo "")
  STATUS=$(rpsql "SELECT status FROM issues WHERE identifier = '$TICKET'")
  HALLUCINATED=$([[ "$STATUS" == "done" && "$FILE_CREATED" == "0" ]] && echo 1 || echo 0)
  TOK_IN=$(rpsql "SELECT COALESCE(SUM((usage_json->>'input_tokens')::int),0) FROM heartbeat_runs WHERE context_snapshot::text LIKE '%$TICKET%'" || echo 0)
  TOK_OUT=$(rpsql "SELECT COALESCE(SUM((usage_json->>'output_tokens')::int),0) FROM heartbeat_runs WHERE context_snapshot::text LIKE '%$TICKET%'" || echo 0)

  echo "  RESULT: file=$FILE_CREATED test=$TEST_CREATED branch=$BRANCH sha=$COMMIT_SHA pr=$PR_URL hallucinated=$HALLUCINATED wall=${WALL}s tok=${TOK_IN}/${TOK_OUT}"
  echo "$MODEL,$BANK,$TICKET,$FILE_CREATED,$TEST_CREATED,$BRANCH,$COMMIT_SHA,$PR_URL,$HALLUCINATED,$WALL,$TOK_IN,$TOK_OUT" >> "$OUT_CSV"
  echo

  # 5. Cleanup — cancel this ticket + any children so next run starts clean
  rpsql "UPDATE issues SET status='cancelled' WHERE identifier = '$TICKET' OR parent_id = (SELECT id FROM issues WHERE identifier = '$TICKET')" >/dev/null || true
done

echo
echo "Bench done. Results at $OUT_CSV"
column -t -s, "$OUT_CSV"
