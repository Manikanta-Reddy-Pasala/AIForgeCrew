#!/usr/bin/env bash
# scripts/benchmark-sr-dev-models.sh — benchmark multiple MLX models as Sr
# Developer, one at a time, with full unload/load cycle. Each model gets an
# isolated ticket (different bank) + 50GB RAM budget (enforced via context
# length pick per model).
#
# Workflow per model:
#   1. Unload ALL currently-loaded LM Studio models
#   2. Load the candidate with the per-model CTX_MAP entry
#   3. Update Paperclip Sr Dev adapter_config.model
#   4. Kill any stale hermes processes
#   5. Create a fresh ticket (bank N)
#   6. Poll issue status every 60s until done/cancelled/timeout
#   7. Record metrics to docs/eval/sr-dev-bench.csv:
#        file_created / test_created / branch / commit_sha / pr_url /
#        hallucinated / wall_seconds / input_tok / output_tok
#   8. Cancel ticket + cleanup
#
# Tester + Sr Arch + EM are expected to be paused. Use:
#   UPDATE agents SET status='paused' WHERE role IN ('qa','cto','pm');
#   UPDATE agents SET runtime_config = jsonb_set(runtime_config,
#     '{heartbeat,enabled}', 'false'::jsonb) WHERE role IN ('qa','cto','pm');
#
# Usage:
#   bash scripts/benchmark-sr-dev-models.sh [SSH_HOST]
set -euo pipefail

# Each entry: "MODEL_ID|CTX|FRIENDLY_NAME"
# CTX picked per model so weights + KV cache ≤ ~50 GB at fp16 KV, or lower
# if the model is huge (Qwen3-Coder-Next 80B uses 32K, others go bigger).
declare -a CANDIDATES=(
  # qwen3.6-35b-a3b baseline already captured (PR#2, ea2830e). Skip on re-run.
  "qwen3-coder-next|65536|Qwen3-Coder-Next-80B-A3B"          # 45GB + 12GB KV = 57GB
  "gemma-4-31b-it|131072|Gemma-4-31B-dense"                  # 17GB + 24GB KV = 41GB (best Gemma 4)
  "gemma-4-26b-a4b-it|131072|Gemma-4-26B-A4B-MoE"            # 16GB + 12GB KV = 28GB
  "deepseek-coder-v2-lite-instruct-mlx|131072|DeepSeek-Coder-V2-Lite-16B-A2.4B"
  "qwen3.5-9b-mlx|131072|Qwen3.5-9B"
)

declare -a BANKS=(PNB CANARA BOB CITI INDUSIND)

SSH_HOST="${1:-manikanta@192.168.70.185}"
CID="${CID:-fd294bd0-2f65-405f-b443-fb41d66226fb}"
SRDEV="${SRDEV:-28b8c064-bfcf-44e1-9e91-e37c39e0097c}"
WAIT_MINUTES="${WAIT_MINUTES:-25}"
OUT_CSV="${OUT_CSV:-docs/eval/sr-dev-bench.csv}"

mkdir -p "$(dirname "$OUT_CSV")"
[[ -f "$OUT_CSV" ]] || echo "model,ctx,bank,ticket,file_created,test_created,branch,commit_sha,pr_url,hallucinated,wall_seconds,input_tokens,output_tokens,final_status" > "$OUT_CSV"

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }
rpsql()  { remote "PGPASSWORD=paperclip \$HOME/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"$1\""; }

for i in "${!CANDIDATES[@]}"; do
  IFS='|' read -r MODEL CTX FRIENDLY <<< "${CANDIDATES[$i]}"
  BANK="${BANKS[$((i % ${#BANKS[@]}))]}"
  BANK_LC=$(echo "$BANK" | tr '[:upper:]' '[:lower:]')

  echo "======================================================================"
  echo " [$((i+1))/${#CANDIDATES[@]}]  $FRIENDLY  @ ctx=$CTX  bank=$BANK"
  echo "======================================================================"

  # 1. Unload everything + kill hermes
  remote "\$HOME/.lmstudio/bin/lms unload --all 2>&1 | tail -3; pkill -f 'hermes chat' 2>/dev/null || true; sleep 3"

  # 2. Load candidate at target ctx
  LOAD_OUT=$(remote "\$HOME/.lmstudio/bin/lms load -y '$MODEL' -c $CTX --gpu max 2>&1 | tail -3" || true)
  echo "  load: $LOAD_OUT"
  if ! echo "$LOAD_OUT" | grep -q "loaded successfully\|To use the model"; then
    echo "  SKIP — load failed for $MODEL"
    echo "$MODEL,$CTX,$BANK,SKIPPED,0,0,,,,0,0,0,0,load_failed" >> "$OUT_CSV"
    continue
  fi

  # Refresh Hermes context cache so it sees the loaded ctx
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
print('cache updated')
PY
"

  # 3. Swap Sr Dev's adapter_config.model
  rpsql "UPDATE agents SET adapter_config = jsonb_set(adapter_config, '{model}', '\\\"$MODEL\\\"'::jsonb) WHERE id = '$SRDEV'" >/dev/null

  # 4. Clear session state so adapter starts fresh
  rpsql "UPDATE agent_runtime_state SET session_id = NULL, last_error = NULL WHERE agent_id = '$SRDEV'" >/dev/null
  rpsql "UPDATE agent_task_sessions SET session_display_id = NULL, session_params_json = '{}'::jsonb WHERE agent_id = '$SRDEV'" >/dev/null

  # Restart paperclip to reload adapter_config
  remote "launchctl kickstart -k gui/\$(id -u)/com.aiforge.paperclip 2>/dev/null; for _ in \$(seq 1 10); do curl -sf http://localhost:3100/api/health >/dev/null 2>&1 && break; sleep 2; done"

  # 5. Create ticket assigned directly to Sr Dev
  START=$(date +%s)
  TITLE="Add $BANK bank OCR parser"
  DESC="Add $BANK bank OCR parser to PosPythonBackend.

Target file: ~/codeRepo/PosPythonBackend/app/util/${BANK_LC}_bank_handler.py
Dispatcher: register in ~/codeRepo/PosPythonBackend/app/routes/pythonBankOCR.py
Test file: ~/codeRepo/PosPythonBackend/tests/util/test_${BANK_LC}_bank_handler.py

Acceptance: write handler + pytest tests, git checkout -b aiforge/ONE-N-${BANK_LC}-bank-ocr, git commit + push, gh pr create --base master. Post commit SHA + PR URL in a comment before marking done. If you claim done without actually creating the files, you are lying; Paperclip will verify."

  PAYLOAD_FILE=/tmp/bench-payload-$$.json
  remote "~/.hermes/hermes-agent/venv/bin/python3 -c \"
import json
print(json.dumps({
  'title': '$TITLE',
  'description': '''$DESC''',
  'priority': 'high',
  'status': 'todo',
  'assigneeAgentId': '$SRDEV',
}))\" > $PAYLOAD_FILE"
  TICKET=$(remote "curl -s -X POST 'http://localhost:3100/api/companies/$CID/issues' -H 'Content-Type: application/json' --data @$PAYLOAD_FILE | ~/.hermes/hermes-agent/venv/bin/python3 -c 'import sys,json; d=json.loads(sys.stdin.read()); print(d.get(\"identifier\",\"?\"))'")
  echo "  ticket: $TICKET"

  # 6. Wait for terminal state
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

  # 7. Collect metrics
  FILE=$(remote "test -f ~/codeRepo/PosPythonBackend/app/util/${BANK_LC}_bank_handler.py && echo 1 || echo 0")
  TEST=$(remote "test -f ~/codeRepo/PosPythonBackend/tests/util/test_${BANK_LC}_bank_handler.py && echo 1 || echo 0")
  BRANCH=$(remote "cd ~/codeRepo/PosPythonBackend && git branch -a 2>/dev/null | grep -o 'aiforge/[^ ]*${BANK_LC}[^ ]*' | head -1" || true)
  SHA=$(remote "cd ~/codeRepo/PosPythonBackend && git log --all --grep='$TICKET' -1 --format=%H 2>/dev/null" || true)
  PR_URL=$(rpsql "SELECT url FROM issue_work_products WHERE issue_id = (SELECT id FROM issues WHERE identifier = '$TICKET') AND type = 'pull_request' LIMIT 1" || true)
  HALLUCINATED=$([[ "$FINAL_ST" == "done" && "$FILE" == "0" ]] && echo 1 || echo 0)
  TOK_IN=$(rpsql "SELECT COALESCE(SUM((usage_json->>'input_tokens')::int),0) FROM heartbeat_runs WHERE context_snapshot::text LIKE '%$TICKET%'" || echo 0)
  TOK_OUT=$(rpsql "SELECT COALESCE(SUM((usage_json->>'output_tokens')::int),0) FROM heartbeat_runs WHERE context_snapshot::text LIKE '%$TICKET%'" || echo 0)

  echo "  RESULT: status=$FINAL_ST file=$FILE test=$TEST sha=$SHA pr=$PR_URL halluc=$HALLUCINATED wall=${WALL}s tok=${TOK_IN}/${TOK_OUT}"
  echo "$MODEL,$CTX,$BANK,$TICKET,$FILE,$TEST,$BRANCH,$SHA,$PR_URL,$HALLUCINATED,$WALL,$TOK_IN,$TOK_OUT,$FINAL_ST" >> "$OUT_CSV"

  # 8. Cleanup for next run
  rpsql "UPDATE issues SET status = 'cancelled' WHERE identifier = '$TICKET'" >/dev/null
  remote "pkill -f 'hermes chat' 2>/dev/null || true"
  echo
done

echo
echo "=============== bench done ==============="
column -t -s, "$OUT_CSV"
