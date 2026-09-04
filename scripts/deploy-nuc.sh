#!/usr/bin/env bash
# Deploy AIForge to the NUC host API (:8799). Idempotent + persistent.
#
# MUST run in a LOGIN shell so `uv` is on PATH:
#   ssh ai@192.168.70.115 'bash -lc "$HOME/AIForgeCrew/scripts/deploy-nuc.sh"'
#
# sudo is passwordless for docker on this host; run.sh brings up the docker
# infra (postgres/neo4j/embed/rerank) then the host uvicorn with the right env.
set -u
REPO="${AIFORGE_REPO:-$HOME/AIForgeCrew}"
cd "$REPO" || { echo "no repo at $REPO"; exit 1; }

echo "==> pull"
git pull --ff-only 2>&1 | tail -1

echo "==> stop existing API"
P=$(ss -tlnp 2>/dev/null | grep ':8799 ' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
[[ -n "${P:-}" ]] && kill -9 "$P" 2>/dev/null
pkill -9 -f "run.sh --host" 2>/dev/null
pkill -9 -f "uvicorn aiforge_core.api" 2>/dev/null
sleep 4

echo "==> launch (detached, persistent)"
setsid nohup bash "$REPO/run.sh" --host 0.0.0.0 --port 8799 \
  >/tmp/aiforge-hybrid.log 2>&1 </dev/null &
disown 2>/dev/null || true

echo "==> wait for :8799"
for i in $(seq 1 30); do
  if ss -tln 2>/dev/null | grep -q ':8799 '; then
    sleep 2
    code=$(curl -s -m6 -o /dev/null -w '%{http_code}' http://127.0.0.1:8799/api/health 2>/dev/null)
    echo "OK: bound :8799 after ~$((i*5))s (health $code)"
    exit 0
  fi
  sleep 5
done
echo "FAIL: :8799 not bound after ~150s"
tail -12 /tmp/aiforge-hybrid.log
exit 1
