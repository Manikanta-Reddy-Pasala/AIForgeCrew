#!/usr/bin/env bash
# One-command NUC deploy + self-test. Reproducible, in-repo (public-repo safe).
# Run from any host that can SSH the NUC, OR on the NUC itself (LOCAL=1).
#
#   AIFORGE_NUC_HOST=192.168.70.115 scripts/deploy-nuc.sh      # via ssh (ai@host)
#   LOCAL=1 scripts/deploy-nuc.sh                              # on the NUC itself
#
# Env: AIFORGE_NUC_USER=ai  AIFORGE_NUC_DIR=~/AIForgeCrew  AIFORGE_NUC_PORT=8799
set -euo pipefail
HOST="${AIFORGE_NUC_HOST:-192.168.70.115}"
USER_="${AIFORGE_NUC_USER:-ai}"
DIR="${AIFORGE_NUC_DIR:-~/AIForgeCrew}"
PORT="${AIFORGE_NUC_PORT:-8799}"

# The remote (or local) deploy + verify body — pull, rebuild, wait, health-check.
read -r -d '' BODY <<REMOTE || true
set -e
cd $DIR
echo "==> git pull"; git pull --ff-only
echo "==> docker compose up -d --build"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
sudo \$DC up -d --build
echo "==> waiting for /api/health (up to 90s)"
for i in \$(seq 1 45); do
  code=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/api/health || echo 000)
  [ "\$code" = "200" ] && { echo "==> api healthy (\$code)"; break; }
  sleep 2
done
echo "==> services:"; sudo \$DC ps
echo "==> health:"; curl -s http://127.0.0.1:$PORT/api/health || echo "(health unreachable)"
echo; echo "==> memory backend (should show postgres/neo4j, not sqlite):"
curl -s http://127.0.0.1:$PORT/api/memory/stats || true
echo; echo "==> DONE. HEAD: \$(git rev-parse --short HEAD)"
REMOTE

if [[ "${LOCAL:-0}" == "1" ]]; then
  echo "==> deploying locally on this host"
  bash -c "$BODY"
else
  echo "==> deploying to $USER_@$HOST"
  ssh -o ConnectTimeout=8 "$USER_@$HOST" "$BODY"
fi
