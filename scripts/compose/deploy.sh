#!/usr/bin/env bash
# Deploy / redeploy the full compose stack on this host.
#
#   scripts/compose/deploy.sh            # pull main, rebuild, recreate, verify
#   scripts/compose/deploy.sh --no-pull  # skip git pull (deploy local tree)
#
# Reads .env in the repo root for host-specific paths (NEO4J_DATA_DIR,
# BGE_M3_HOST_DIR, AIFORGE_LM_BASE_URL, ...). DOCKER="sudo docker" if your
# user isn't in the docker group.
set -euo pipefail

cd "$(dirname "$0")/../.."
DOCKER="${DOCKER:-sudo docker}"
PULL=1
[[ "${1:-}" == "--no-pull" ]] && PULL=0

if [[ $PULL -eq 1 ]]; then
  echo "==> git pull"
  git pull --ff-only origin main
fi

echo "==> build + recreate (changed services only)"
$DOCKER compose up -d --build

echo "==> status"
$DOCKER compose ps

echo "==> health"
for _ in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8799/api/health >/dev/null 2>&1; then
    curl -s http://127.0.0.1:8799/api/health; echo; break
  fi
  sleep 2
done
