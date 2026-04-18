#!/usr/bin/env bash
# scripts/sync-code-repos.sh — rsync ~/Documents/codeRepo/ to Mac Studio.
# Skips .venv/, node_modules/, .git/objects packs that pull without them.
# Target = ~/codeRepo/ on Mac Studio (per user request).
set -euo pipefail

SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
SRC="${SRC:-$HOME/Documents/codeRepo/}"
DST_DIR="${DST_DIR:-codeRepo}"   # relative to Mac Studio $HOME

[[ -d "$SRC" ]] || { echo "source missing: $SRC" >&2; exit 1; }

ssh "$SSH_HOST" "mkdir -p ~/$DST_DIR"

echo ">>> rsync $SRC → $SSH_HOST:~/$DST_DIR/"
rsync -avhP --partial --inplace \
  --exclude '.venv/' \
  --exclude 'node_modules/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '.mypy_cache/' \
  --exclude '.ruff_cache/' \
  --exclude '.idea/' \
  --exclude '.vscode/' \
  --exclude 'target/' \
  --exclude 'build/' \
  --exclude 'dist/' \
  --exclude '*.class' \
  --exclude '*.pyc' \
  "$SRC" "$SSH_HOST:\$HOME/$DST_DIR/"

echo
echo "Done. Verify:"
echo "  ssh $SSH_HOST 'ls ~/$DST_DIR | head'"
