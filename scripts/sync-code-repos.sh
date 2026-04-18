#!/usr/bin/env bash
# scripts/sync-code-repos.sh — rsync laptop ~/Documents/codeRepo/ → Mac Studio ~/codeRepo/.
# Remote path resolved via remote $HOME to avoid user-mismatch issues.
set -euo pipefail

SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
SRC="${SRC:-$HOME/Documents/codeRepo/}"
DST_SUB="${DST_SUB:-codeRepo}"   # relative to remote $HOME

[[ -d "$SRC" ]] || { echo "source missing: $SRC" >&2; exit 1; }

REMOTE_HOME=$(ssh "$SSH_HOST" 'printf %s "$HOME"')
[[ -n "$REMOTE_HOME" ]] || { echo "could not resolve remote \$HOME" >&2; exit 1; }

DST="$REMOTE_HOME/$DST_SUB/"
ssh "$SSH_HOST" "mkdir -p '$DST'"

echo ">>> rsync $SRC → $SSH_HOST:$DST"
rsync -ahP --partial --inplace \
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
  "$SRC" "$SSH_HOST:$DST"

echo
echo "Done. Verify:"
echo "  ssh $SSH_HOST 'ls $DST | head'"
