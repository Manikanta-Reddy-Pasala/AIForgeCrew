#!/usr/bin/env bash
# scripts/sync-memory.sh — rsync Claude memory + claude-mem DB between laptop and Mac Studio.
# Path-agnostic: derives remote path from the remote $HOME, not the local one.
#
# Per-subpath rsync options:
#   .claude/memory/  — --delete OK (topic md files are shared canon)
#   .claude/projects/ — NO --delete (each machine has unique session state)
#   .claude-mem/     — --delete OK (derived index, regenerable)
#
# Usage:
#   bash scripts/sync-memory.sh                # DIR=push (default, laptop → Mac Studio)
#   DIR=pull bash scripts/sync-memory.sh
#   DIR=both bash scripts/sync-memory.sh
set -euo pipefail

SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
DIR="${DIR:-push}"

REMOTE_HOME=$(ssh "$SSH_HOST" 'printf %s "$HOME"')
[[ -n "$REMOTE_HOME" ]] || { echo "could not resolve remote \$HOME" >&2; exit 1; }

# (subpath, rsync-opts) pairs. projects/ uses additive sync to preserve
# remote-only session data (Manikanta's Paperclip + Mac Studio sessions).
SUBPATHS=(
  ".claude/memory/::-ahP --delete --partial --inplace"
  ".claude/projects/::-ahP --partial --inplace"
  ".claude-mem/::-ahP --delete --partial --inplace"
)

push_one() {
  local entry="$1"
  local sub="${entry%%::*}" opts="${entry##*::}"
  local src="$HOME/$sub"
  local dst="$SSH_HOST:$REMOTE_HOME/$sub"
  [[ -d "$src" ]] || { echo "[skip] $src (missing locally)"; return; }
  echo ">>> push $sub  (opts: $opts)"
  ssh "$SSH_HOST" "mkdir -p '$REMOTE_HOME/$sub'"
  rsync $opts "$src" "$dst"
}

pull_one() {
  local entry="$1"
  local sub="${entry%%::*}" opts="${entry##*::}"
  local src="$SSH_HOST:$REMOTE_HOME/$sub"
  local dst="$HOME/$sub"
  ssh "$SSH_HOST" "test -d '$REMOTE_HOME/$sub'" || { echo "[skip] $sub (missing on remote)"; return; }
  echo ">>> pull $sub  (opts: $opts)"
  mkdir -p "$dst"
  rsync $opts "$src" "$dst"
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
