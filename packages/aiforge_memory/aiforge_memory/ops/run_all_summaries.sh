#!/usr/bin/env bash
set -uo pipefail
set -a; source ~/.aiforge/runtime.env; set +a

REPOS=(
  PosClientBackend
  oneshell-commons
  PosService
  MongoDbService
  PosDockerSyncService
  BusinessService
  NotificationService
  Scheduler
  StoreIntelligence
  GstApiService
  WhatsappApiService
  EmailService
  TallyConnector
  GatewayService
  PosServerBackend
  PosDataSyncService
  PosDockerPullService
  QuartzScheduler
  VendorIntegrationService
  mongoEventListner
  CacheLayer
  memory
  keycloak-two-factor-auth-extension
)

LOGDIR=/home/mani/.aiforge/symsum_logs
mkdir -p "$LOGDIR"
SSH_MS='ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 manikanta@192.168.70.185'
LMS=/Users/manikanta/.lmstudio/bin/lms
MODEL_REF='qwen/qwen3-coder-next'
MODEL_ID='qwen3-coder'

restart_lms() {
  echo "[$(date)] LM-Studio restart: unload+reload $MODEL_ID" | tee -a $LOGDIR/master.log
  $SSH_MS "$LMS unload $MODEL_ID 2>&1 | tail -1" >> $LOGDIR/master.log 2>&1 || true
  sleep 3
  $SSH_MS "$LMS load '$MODEL_REF' --identifier $MODEL_ID --gpu max --ttl 86400 -y 2>&1 | tail -2" >> $LOGDIR/master.log 2>&1
  sleep 6
  # PONG sanity — drop into the script's stderr if not OK
  if ! curl -sS --max-time 30 -X POST http://127.0.0.1:1234/v1/chat/completions \
       -H 'Content-Type: application/json' \
       -d "{\"model\":\"$MODEL_ID\",\"messages\":[{\"role\":\"user\",\"content\":\"PONG\"}],\"max_tokens\":3}" \
       | grep -q chatcmpl; then
    echo "[$(date)] LM restart sanity FAILED" | tee -a $LOGDIR/master.log
    return 1
  fi
  echo "[$(date)] LM restart sanity OK" | tee -a $LOGDIR/master.log
}

run_repo() {
  local repo="$1"
  local log="$LOGDIR/$repo.log"
  echo "[$(date)] === $repo ===" | tee -a $LOGDIR/master.log
  ~/codeRepo/AiForgeMemory/.venv/bin/aiforge-memory summarise-symbols "$repo" --min-lines 12 > "$log" 2>&1
  return $?
}

echo "[$(date)] starting summaries for ${#REPOS[@]} repos" | tee -a $LOGDIR/master.log

for repo in "${REPOS[@]}"; do
  run_repo "$repo"
  rc=$?
  tail -1 $LOGDIR/$repo.log | tee -a $LOGDIR/master.log

  # Auto-recover on abort (rc=2 = SymbolSummaryAborted: LM wedged).
  attempt=1
  while [ $rc -eq 2 ] && [ $attempt -le 3 ]; do
    echo "[$(date)] $repo: aborted (attempt $attempt) — restarting LM Studio" | tee -a $LOGDIR/master.log
    if restart_lms; then
      run_repo "$repo"
      rc=$?
      tail -1 $LOGDIR/$repo.log | tee -a $LOGDIR/master.log
    else
      sleep 30
    fi
    attempt=$((attempt + 1))
  done

  if [ $rc -ne 0 ] && [ $rc -ne 2 ]; then
    echo "[$(date)] $repo: hard error rc=$rc — sleeping 60s" | tee -a $LOGDIR/master.log
    sleep 60
  fi
  sleep 5
done

echo "[$(date)] all done" | tee -a $LOGDIR/master.log
