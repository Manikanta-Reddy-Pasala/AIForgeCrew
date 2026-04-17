#!/usr/bin/env bash
# scripts/health-check.sh — probe local services, exit non-zero on failure.
set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) echo "Usage: health-check.sh [--dry-run]"; exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

probes=(
  "paperclip|http://localhost:8900/health"
  "hermes|http://localhost:8910/health"
  "mem0|http://localhost:8920/health"
  "rag|http://localhost:8930/health"
  "local-llm|http://localhost:11434/api/tags"
)

status=0
for probe in "${probes[@]}"; do
  name="${probe%%|*}"
  url="${probe##*|}"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "would probe ${name} @ ${url}"
    continue
  fi
  if curl -fsS --max-time 3 "${url}" >/dev/null 2>&1; then
    echo "OK  ${name}"
  else
    echo "FAIL ${name} (${url})" >&2
    status=1
  fi
done

if [[ $DRY_RUN -eq 1 ]]; then
  echo "dry-run OK"
fi
exit $status
