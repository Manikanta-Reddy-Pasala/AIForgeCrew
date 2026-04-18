#!/usr/bin/env bash
# scripts/paperclip-start.sh — start Paperclip server (bg) + wait ready.
set -euo pipefail

# Reuse Hermes's bundled Node 22 if present.
export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"

LOG_DIR="${LOG_DIR:-$HOME/aiforge-logs}"
mkdir -p "$LOG_DIR"

if curl -s -o /dev/null -f http://localhost:3100/api/health 2>/dev/null; then
  echo "Paperclip already running on :3100"
  exit 0
fi

command -v npx >/dev/null || { echo "npx missing — run: make hermes-install (provides Node 22)" >&2; exit 1; }

# Correct command is `run` (not `start`).
nohup bash -c "npx -y paperclipai run" > "$LOG_DIR/paperclip.log" 2>&1 &
echo "Paperclip starting, log=$LOG_DIR/paperclip.log"

for _ in $(seq 1 60); do
  if curl -s -o /dev/null -f http://localhost:3100/api/health 2>/dev/null; then
    echo "Paperclip UI ready at http://localhost:3100"
    exit 0
  fi
  sleep 1
done

echo "Paperclip did not become ready in 60s — check $LOG_DIR/paperclip.log" >&2
tail -30 "$LOG_DIR/paperclip.log" >&2 || true
exit 1
