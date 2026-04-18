#!/usr/bin/env bash
# scripts/hermes-dashboard-start.sh — start Hermes web dashboard at :9119.
# macOS-only. Binds loopback; access via `make hermes-dashboard-tunnel`.
set -euo pipefail

export PATH="$HOME/.local/bin:$HOME/.hermes/node/bin:$PATH"
LOG_DIR="${LOG_DIR:-$HOME/aiforge-logs}"
mkdir -p "$LOG_DIR"

command -v hermes >/dev/null || { echo "hermes CLI missing — run: make hermes-install" >&2; exit 1; }

if curl -s -o /dev/null -f http://127.0.0.1:9119/ 2>/dev/null; then
  echo "Hermes dashboard already running on :9119"
  exit 0
fi

nohup bash -c 'hermes dashboard --no-open --port 9119' > "$LOG_DIR/hermes-dashboard.log" 2>&1 &
echo "Hermes dashboard starting, log=$LOG_DIR/hermes-dashboard.log"

for _ in $(seq 1 30); do
  if curl -s -o /dev/null -f http://127.0.0.1:9119/ 2>/dev/null; then
    echo "Dashboard ready at http://127.0.0.1:9119"
    exit 0
  fi
  sleep 1
done

echo "Dashboard did not become ready in 30s — check $LOG_DIR/hermes-dashboard.log" >&2
tail -30 "$LOG_DIR/hermes-dashboard.log" >&2 || true
exit 1
