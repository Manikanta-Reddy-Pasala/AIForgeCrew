#!/usr/bin/env bash
# scripts/install-mempalace.sh — install MemPalace + init 5 palaces (1 shared + 4 per-role).
# Mac-only. Idempotent.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install-mempalace: macOS only" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

UV="${UV:-$HOME/.local/bin/uv}"; command -v "$UV" >/dev/null || UV=uv

if [[ ! -d .venv ]]; then
  echo "run scripts/install-paperclip.sh first (need .venv)" >&2
  exit 1
fi

echo ">>> installing mempalace into .venv"
"$UV" pip install --python .venv/bin/python mempalace chromadb

BASE=".aiforge/mem"
mkdir -p "$BASE"
for scope in project agent/em agent/tester agent/sr-developer agent/sr-architect; do
  PALACE="$BASE/$scope"
  if [[ -f "$PALACE/config.json" ]]; then
    echo "  [skip] $scope (already initialized)"
    continue
  fi
  mkdir -p "$PALACE"
  echo ">>> init palace: $scope"
  .venv/bin/mempalace --palace "$PALACE" init "$PALACE" --yes </dev/null || {
    echo "  WARN: $scope init had non-zero exit (may be benign)" >&2
  }
done

echo "mempalace installed. Palaces under $BASE/"
