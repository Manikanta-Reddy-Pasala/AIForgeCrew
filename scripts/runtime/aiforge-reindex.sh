#!/usr/bin/env bash
# Reindex hook — guarded so it never runs inside an aiforge worktree.
set -uo pipefail
REPO_DIR="${1:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -z "${REPO_DIR}" ] && exit 0
case "$REPO_DIR" in
  *.aiforge-worktrees/*) exit 0 ;;
esac
REPO_NAME="$(basename "$REPO_DIR")"
LOG_DIR="${HOME}/.aiforge/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/reindex-${REPO_NAME}.log"
PY="${AIFORGE_VENV:-/home/mani/AIForgeCrew/.venv}/bin/python"
GRAPHIFY="${GRAPHIFY:-/home/mani/.local/bin/graphify}"
(
  echo "=== $(date -Iseconds) reindex $REPO_NAME ==="
  if [ -x "$GRAPHIFY" ]; then
    cd "$REPO_DIR" && timeout 600 "$GRAPHIFY" update . 2>&1 | tail -3
  fi
  GJ="$REPO_DIR/graphify-out/graph.json"
  if [ -f "$GJ" ] && [ -x "$PY" ]; then
    AIFORGE_NEO4J_URI="${AIFORGE_NEO4J_URI:-bolt://127.0.0.1:7687}" \
      "$PY" -m aiforge_core.index.graphify_loader --graph "$GJ" --repo "$REPO_NAME" 2>&1 | tail -3
  fi
  echo "=== done $SECONDS s ==="
) >>"$LOG" 2>&1 &
disown
exit 0
