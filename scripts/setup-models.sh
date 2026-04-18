#!/usr/bin/env bash
# scripts/setup-models.sh — orchestrator: download → verify → start server → health check.
# Idempotent. Safe to re-run. Works locally or over ssh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

DRY_RUN=0
SKIP_DOWNLOAD=0
SKIP_VERIFY=0
SKIP_SERVER=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)       DRY_RUN=1 ;;
    --skip-download) SKIP_DOWNLOAD=1 ;;
    --skip-verify)   SKIP_VERIFY=1 ;;
    --skip-server)   SKIP_SERVER=1 ;;
    -h|--help)
      cat <<'H'
Usage: setup-models.sh [--dry-run] [--skip-download] [--skip-verify] [--skip-server]

Orchestrates P0 model setup:
  1. scripts/download-models.sh    (reads security/model-checksums.yml)
  2. scripts/verify-checksums.sh   (enforces sha256)
  3. scripts/start-servers.sh      (LM Studio OpenAI-compat on :1234)
  4. scripts/health-check.sh       (probe per-role inference)
H
      exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

run() {
  echo ">>> $*"
  if [[ $DRY_RUN -eq 1 ]]; then return 0; fi
  "$@"
}

[[ $SKIP_DOWNLOAD -eq 0 ]] && run bash scripts/download-models.sh
[[ $SKIP_VERIFY   -eq 0 ]] && run bash scripts/verify-checksums.sh
[[ $SKIP_SERVER   -eq 0 ]] && run bash scripts/start-servers.sh
run bash scripts/load-models.sh
run bash scripts/health-check.sh

echo "setup-models.sh: OK"
