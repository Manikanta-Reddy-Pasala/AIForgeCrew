#!/usr/bin/env bash
# scripts/sync-memory.sh — rsync Claude memory + claude-mem DB between laptop and Mac Studio.
# Direction defaults to "push" (laptop → Mac Studio).
#
# Usage:
#   bash scripts/sync-memory.sh                # push
#   DIR=pull bash scripts/sync-memory.sh       # pull from Mac Studio
#   DIR=both bash scripts/sync-memory.sh       # push then pull (last-writer-wins per file)
set -euo pipefail

SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
DIR="${DIR:-push}"

PATHS=(
  "$HOME/.claude/memory/"
  "$HOME/.claude/projects/"
  "$HOME/.claude-mem/"
)

RSYNC_OPTS=(-avhP --delete --partial --inplace)

push_one() {
  local src="$1"
  local dst="$SSH_HOST:$src"
  [[ -d "$src" ]] || { echo "[skip] $src (missing locally)"; return; }
  echo ">>> push $src → $SSH_HOST"
  ssh "$SSH_HOST" "mkdir -p '$src'"
  rsync "${RSYNC_OPTS[@]}" "$src" "$dst"
}

pull_one() {
  local src="$1"
  local dst="$SSH_HOST:$src"
  ssh "$SSH_HOST" "test -d '$src'" || { echo "[skip] $src (missing on remote)"; return; }
  echo ">>> pull $dst → $src"
  mkdir -p "$src"
  rsync "${RSYNC_OPTS[@]}" "$dst" "$src"
}

case "$DIR" in
  push)  for p in "${PATHS[@]}"; do push_one "$p"; done ;;
  pull)  for p in "${PATHS[@]}"; do pull_one "$p"; done ;;
  both)
    for p in "${PATHS[@]}"; do push_one "$p"; done
    for p in "${PATHS[@]}"; do pull_one "$p"; done
    ;;
  *) echo "DIR must be push|pull|both" >&2; exit 2 ;;
esac

echo
echo "Memory sync complete ($DIR)."
