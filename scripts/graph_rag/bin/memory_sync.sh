#!/usr/bin/env bash
# Push local Claude memory directories to the shared memory-repo GitHub clone.
# Launchd + cron friendly; exits 0 cleanly with nothing to do.
set -euo pipefail

MEM_REPO="${MEM_REPO:-$HOME/.claude/memory-repo}"
SRC_ROOT="${SRC_ROOT:-$HOME/.claude/projects}"

if [ ! -d "$MEM_REPO/.git" ]; then
  echo "ERROR: $MEM_REPO is not a git repo. Run:"
  echo "  mkdir -p $MEM_REPO && cd $MEM_REPO && git init && \\"
  echo "    git remote add origin git@github.com:<you>/claude-memories.git"
  exit 1
fi

cd "$MEM_REPO"
for d in "$SRC_ROOT"/*/memory; do
  [ -d "$d" ] || continue
  proj="$(basename "$(dirname "$d")")"
  mkdir -p "$proj"
  rsync -a --delete "$d/" "$proj/"
done

git add -A
if ! git diff --cached --quiet; then
  git commit -m "memory: $(date -u +%FT%TZ) auto sync from $(hostname)"
  git push origin HEAD
  echo "pushed"
else
  echo "no changes"
fi
