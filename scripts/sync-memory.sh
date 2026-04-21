#!/usr/bin/env bash
# scripts/sync-memory.sh — push laptop MD files + claude-memory to Mac Studio,
# then rebuild the affected memory wings so agents pick up the latest content.
#
# What it does:
#   1. rsync  ~/.claude/memory/      → Mac Studio ~/.claude/memory/
#   2. rsync  ~/Documents/codeRepo/CLAUDE.md → Mac Studio ~/codeRepo/CLAUDE.md
#   3. rsync  ~/Documents/codeRepo/*/CLAUDE.md → Mac Studio ~/codeRepo/*/CLAUDE.md
#   4. remote: reindex wings  claude-memory  project-rules  aiforge
#      (clears the wing, re-chunks + re-embeds via bge-m3 sidecar).
#
# Usage:
#   bash scripts/sync-memory.sh
#   SSH_HOST=user@host bash scripts/sync-memory.sh
#
# Optional flags:
#   REINDEX_ONLY=1 bash scripts/sync-memory.sh   # skip rsync, just rebuild
#   RSYNC_ONLY=1   bash scripts/sync-memory.sh   # skip reindex
set -euo pipefail

SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
LAPTOP_REPO="${LAPTOP_REPO:-$HOME/Documents/codeRepo}"

if [[ "${RSYNC_ONLY:-0}" != "1" && "${REINDEX_ONLY:-0}" != "1" ]]; then
  :
fi

# ---- 1. push MD files ----
if [[ "${REINDEX_ONLY:-0}" != "1" ]]; then
  echo ">>> rsync ~/.claude/memory → $SSH_HOST"
  rsync -a --delete --exclude=.DS_Store \
    "$HOME/.claude/memory/" "$SSH_HOST:~/.claude/memory/"

  echo ">>> rsync top-level CLAUDE.md"
  if [[ -f "$LAPTOP_REPO/CLAUDE.md" ]]; then
    rsync -a "$LAPTOP_REPO/CLAUDE.md" "$SSH_HOST:~/codeRepo/CLAUDE.md"
  fi

  echo ">>> rsync per-repo CLAUDE.md files"
  for md in "$LAPTOP_REPO"/*/CLAUDE.md; do
    [[ -f "$md" ]] || continue
    repo=$(basename "$(dirname "$md")")
    rsync -a "$md" "$SSH_HOST:~/codeRepo/$repo/CLAUDE.md" 2>/dev/null || true
  done
fi

# ---- 2. reindex on Mac Studio ----
if [[ "${RSYNC_ONLY:-0}" != "1" ]]; then
  echo ">>> reindex on $SSH_HOST"
  ssh "$SSH_HOST" 'cd ~/AIForgeCrew && .venv/bin/python - <<'"'"'PY'"'"'
from pathlib import Path
from aiforge_core.store_v2 import Store
from aiforge_core.rag import reindex_repo

s = Store(); s.ensure_schema()
jobs = [
    ("claude-memory",  Path.home() / ".claude",    ["memory/**/*.md","CLAUDE.md","*.md"]),
    ("project-rules",  Path.home() / "codeRepo",   ["CLAUDE.md","*/CLAUDE.md"]),
    ("aiforge",        Path.home() / "AIForgeCrew",["aiforge_core/**/*.py","scripts/**/*.sh","scripts/**/*.py","docs/**/*.md","*.md","Makefile","pyproject.toml"]),
]
for repo, root, sources in jobs:
    if not root.exists():
        print(f"  - {repo}: {root} MISSING — skipped"); continue
    r = reindex_repo(s, repo=repo, repo_root=root, sources=sources)
    print(f"  + {repo:<15} {r.files:>3} files  {r.chunks:>4} chunks")
PY
'
fi

echo ">>> done"
