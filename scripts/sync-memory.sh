#!/usr/bin/env bash
# scripts/sync-memory.sh — rsync Claude memory + claude-mem DB between laptop and Mac Studio.
# Path-agnostic: derives remote path from the remote $HOME, not the local one.
#
# Usage:
#   bash scripts/sync-memory.sh                # DIR=push (default, laptop → Mac Studio)
#   DIR=pull bash scripts/sync-memory.sh
#   DIR=both bash scripts/sync-memory.sh
set -euo pipefail

SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
DIR="${DIR:-push}"

# Discover remote $HOME once so we never assume /Users/<local-user> on remote.
REMOTE_HOME=$(ssh "$SSH_HOST" 'printf %s "$HOME"')
[[ -n "$REMOTE_HOME" ]] || { echo "could not resolve remote \$HOME" >&2; exit 1; }

# Source = local absolute, Dest = remote absolute under REMOTE_HOME.
# Each entry is "<local-subpath-under-\$HOME>".
SUBPATHS=(
  ".claude/memory/"
  ".claude/projects/"
  ".claude-mem/"
)

RSYNC_OPTS=(-ahP --delete --partial --inplace)

push_one() {
  local sub="$1"
  local src="$HOME/$sub"
  local dst="$SSH_HOST:$REMOTE_HOME/$sub"
  [[ -d "$src" ]] || { echo "[skip] $src (missing locally)"; return; }
  echo ">>> push $sub"
  ssh "$SSH_HOST" "mkdir -p '$REMOTE_HOME/$sub'"
  rsync "${RSYNC_OPTS[@]}" "$src" "$dst"
}

pull_one() {
  local sub="$1"
  local src="$SSH_HOST:$REMOTE_HOME/$sub"
  local dst="$HOME/$sub"
  ssh "$SSH_HOST" "test -d '$REMOTE_HOME/$sub'" || { echo "[skip] $sub (missing on remote)"; return; }
  echo ">>> pull $sub"
  mkdir -p "$dst"
  rsync "${RSYNC_OPTS[@]}" "$src" "$dst"
}

case "$DIR" in
  push)  for s in "${SUBPATHS[@]}"; do push_one "$s"; done ;;
  pull)  for s in "${SUBPATHS[@]}"; do pull_one "$s"; done ;;
  both)  for s in "${SUBPATHS[@]}"; do push_one "$s"; done
         for s in "${SUBPATHS[@]}"; do pull_one "$s"; done ;;
  *) echo "DIR must be push|pull|both" >&2; exit 2 ;;
esac

echo
echo "Memory sync complete ($DIR). Remote home=$REMOTE_HOME."
