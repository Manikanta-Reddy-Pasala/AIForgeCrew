#!/usr/bin/env bash
# scripts/start-servers.sh — brings up docker-compose services.
set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) echo "Usage: start-servers.sh [--dry-run]"; exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

cmd=(docker compose --file docker-compose.yml up -d)

if [[ $DRY_RUN -eq 1 ]]; then
  echo "would run: ${cmd[*]}"
  exit 0
fi

"${cmd[@]}"
