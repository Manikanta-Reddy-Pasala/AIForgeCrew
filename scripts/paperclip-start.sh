#!/usr/bin/env bash
# scripts/paperclip-start.sh — start Paperclip server + UI in background.
# Uses `nohup` so SSH-initiated start survives the session close.
set -euo pipefail

LOG_DIR="${LOG_DIR:-$HOME/aiforge-logs}"
mkdir -p "$LOG_DIR"

if curl -s -o /dev/null -f http://localhost:3100/health 2>/dev/null; then
  echo "Paperclip already running on :3100"
  exit 0
fi

# Paperclip docs: `paperclipai start` or `npx paperclipai start`.
# Use full-path npx to avoid PATH surprises over SSH.
NPX="$(command -v npx)"
if [[ -z "$NPX" ]]; then
  echo "npx not in PATH — install Node 20+ first" >&2
  exit 1
fi

nohup bash -c "$NPX -y paperclipai start" > "$LOG_DIR/paperclip.log" 2>&1 &
PID=$!
echo "Paperclip starting, PID=$PID, log=$LOG_DIR/paperclip.log"

# Wait up to 30s for readiness.
for _ in $(seq 1 30); do
  if curl -s -o /dev/null -f http://localhost:3100/health 2>/dev/null; then
    echo "Paperclip UI ready at http://localhost:3100"
    exit 0
  fi
  sleep 1
done

echo "Paperclip did not become ready in 30s — check $LOG_DIR/paperclip.log" >&2
exit 1
