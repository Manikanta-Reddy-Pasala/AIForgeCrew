#!/usr/bin/env bash
# Install as .git/hooks/post-merge in each repo + memory-repo. Uses the
# diff between the previous and current HEAD to drive graph_incremental.sh.
set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
CHANGED=$(git diff --name-only HEAD@{1} HEAD -- \
          '*.java' '*.ts' '*.tsx' '*.js' '*.jsx' '*.py' '*.md' 2>/dev/null || true)
[ -z "$CHANGED" ] && exit 0

GR_DIR="${AIFORGE_GRAPH_RAG:-$HOME/Documents/codeRepo/AIForgeCrew/scripts/graph_rag}"

case "$REPO" in
  *memory-repo)
    "${VENV:-$HOME/aiforge-venv}/bin/python" "$GR_DIR/ingest_memory.py" --files $CHANGED
    "${VENV:-$HOME/aiforge-venv}/bin/python" "$GR_DIR/link_memories.py"
    ;;
  *)
    bash "$GR_DIR/bin/graph_incremental.sh" "$REPO" $CHANGED
    ;;
esac
