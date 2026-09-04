#!/usr/bin/env bash
# Reindex hook — runs Graphify + Neo4j mirror for the canonical repo.
#
# DESIGN: NEVER writes inside the canonical code repo or any worktree.
# All outputs land under $HOME/.aiforge/ — RepoMap tags cache, Graphify
# artifacts, logs.
#
# Guards:
#   1. Skip if invoked from inside an .aiforge-worktrees/* path
#      (we only reindex the canonical repo, never doer worktrees).
#   2. Run graphify with --out pointing at $HOME/.aiforge/graphify-out
#      so graph.json + cache stay outside the repo.
#   3. Run loader with that out-of-tree graph.json.
#
# Install symlink-style:
#   ln -sf <abs>/scripts/runtime/aiforge-reindex.sh \
#          <repo>/.git/hooks/post-commit
#   ln -sf <abs>/scripts/runtime/aiforge-reindex.sh \
#          <repo>/.git/hooks/post-merge
set -uo pipefail

REPO_DIR="${1:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[[ -z "${REPO_DIR}" ]] && exit 0

case "$REPO_DIR" in
  *.aiforge-worktrees/*) exit 0 ;;
  *) ;;
esac

REPO_NAME="$(basename "$REPO_DIR")"

# All AIForge artifacts live under $HOME/.aiforge — never in the repo.
AIFORGE_HOME="${AIFORGE_HOME:-$HOME/.aiforge}"
LOG_DIR="$AIFORGE_HOME/logs"
GRAPHIFY_OUT_BASE="$AIFORGE_HOME/graphify-out"
mkdir -p "$LOG_DIR" "$GRAPHIFY_OUT_BASE"

LOG="$LOG_DIR/reindex-$REPO_NAME.log"
GRAPHIFY_OUT="$GRAPHIFY_OUT_BASE/$REPO_NAME"

# Resolve the interpreter + graphify from THIS checkout and the caller's PATH.
# Hardcoded /home/mani/... paths meant that on any other box (the NUC runs as
# `ai`) both were missing, so the hook exited having done nothing — silently,
# because a git hook always exits 0.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIFORGE_REPO="$(cd "$HOOK_DIR/../.." && pwd)"
PY="${AIFORGE_VENV:-$AIFORGE_REPO/.venv}/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"
GRAPHIFY="${GRAPHIFY:-$(command -v graphify || echo "$HOME/.local/bin/graphify")}"

(
  echo "=== $(date -Iseconds) reindex $REPO_NAME ==="
  echo "    out=$GRAPHIFY_OUT"

  if [[ -x "$GRAPHIFY" ]]; then
    cd "$REPO_DIR" && \
      timeout 600 "$GRAPHIFY" update . --out "$GRAPHIFY_OUT" 2>&1 | tail -3
  fi

  # The Neo4j mirror step is gone with the graph layer — aiforge_core.indexing
  # .graphify_loader no longer exists, so calling it only produced "No module
  # named" in a log nobody reads. graphify's own artifacts under
  # $GRAPHIFY_OUT are the output now.
  GJ="$GRAPHIFY_OUT/graph.json"
  [[ -f "$GJ" ]] && echo "    graph: $GJ"

  echo "=== done $SECONDS s ==="
) >>"$LOG" 2>&1 &

disown
exit 0
