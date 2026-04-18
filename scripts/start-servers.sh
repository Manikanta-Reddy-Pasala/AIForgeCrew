#!/usr/bin/env bash
# scripts/start-servers.sh — start LM Studio OpenAI-compat server on :1234 + wait ready.
# Docker-compose services wired in P2; for P0 this manages LM Studio only.
set -euo pipefail

LMS="${LMS:-$HOME/.lmstudio/bin/lms}"
PORT="${PORT:-1234}"
TIMEOUT="${TIMEOUT:-30}"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) echo "Usage: start-servers.sh [--dry-run]"; exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [[ ! -x "$LMS" ]]; then
  echo "LM Studio CLI missing at $LMS — install LM Studio first." >&2
  exit 1
fi

if curl -s -o /dev/null -f "http://localhost:${PORT}/v1/models"; then
  echo "Server already running on :${PORT}"
  exit 0
fi

if [[ $DRY_RUN -eq 1 ]]; then
  echo "would run: $LMS server start"
  exit 0
fi

echo "Starting LM Studio server..."
"$LMS" server start

for _ in $(seq 1 "$TIMEOUT"); do
  if curl -s -o /dev/null -f "http://localhost:${PORT}/v1/models"; then
    echo "Server ready on :${PORT}"
    exit 0
  fi
  sleep 1
done

echo "Server did not become ready within ${TIMEOUT}s" >&2
exit 1
