#!/usr/bin/env bash
# Re-run Aider + Graphify + tree-sitter ingest for the current repo.
# Designed to be invoked from a git post-commit / post-merge hook.
#
# Idempotent + best-effort: any sub-step failing logs a warning but
# the others continue. Total runtime budget: ~5 minutes for a 10k-file
# repo; runs detached via `setsid &` so the git command returns
# immediately.
#
# Usage:
#   /path/to/aiforge-reindex.sh                # auto-detect repo
#   /path/to/aiforge-reindex.sh /repo/path     # explicit path
#
# Install as hook:
#   ln -sf $PWD/scripts/runtime/aiforge-reindex.sh \
#          <repo>/.git/hooks/post-commit
#   ln -sf $PWD/scripts/runtime/aiforge-reindex.sh \
#          <repo>/.git/hooks/post-merge
#
# Or globally via:
#   git config --global core.hooksPath /path/to/aiforge-hooks
set -uo pipefail

REPO_DIR="${1:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -z "${REPO_DIR}" ] && { echo "no git repo"; exit 0; }
REPO_NAME="$(basename "$REPO_DIR")"

LOG_DIR="${HOME}/.aiforge/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/reindex-${REPO_NAME}.log"

AIFORGE_VENV="${AIFORGE_VENV:-/home/mani/AIForgeCrew/.venv}"
[ -d "$AIFORGE_VENV" ] || AIFORGE_VENV="/Users/manikanta/AIForgeCrew/.venv"
PY="$AIFORGE_VENV/bin/python"
GRAPHIFY="${GRAPHIFY:-/home/mani/.local/bin/graphify}"
[ -x "$GRAPHIFY" ] || GRAPHIFY="/Users/manikanta/.local/bin/graphify"

(
  echo "=== $(date -Iseconds) reindex $REPO_NAME ==="

  # 1) Aider tags cache — invalidate so next Doer call rebuilds against
  #    the new tree. Aider auto-rescans changed files via its diskcache.
  TAGS_CACHE="$REPO_DIR/.aider.tags.cache.v4"
  if [ -d "$TAGS_CACHE" ]; then
    # Touch the cache dir so Aider knows it exists; Aider handles
    # incremental refresh via per-file mtime hashing.
    touch "$TAGS_CACHE"
    echo "[aider] tags cache touched ($TAGS_CACHE)"
  else
    echo "[aider] no cache yet — will be built on next Doer call"
  fi

  # 2) Graphify update — incremental rebuild of graph.json under
  #    repo's graphify-out/. v0.4.x uses `graphify update .`.
  if [ -x "$GRAPHIFY" ]; then
    cd "$REPO_DIR"
    timeout 600 "$GRAPHIFY" update . 2>&1 | tail -5
    echo "[graphify] update complete"
  else
    echo "[graphify] CLI not found at $GRAPHIFY — skipping"
  fi

  # 3) Reload graph.json into Neo4j as :File / :Symbol / :CALLS etc.
  GRAPH_JSON="$REPO_DIR/graphify-out/graph.json"
  if [ -f "$GRAPH_JSON" ] && [ -x "$PY" ]; then
    AIFORGE_NEO4J_URI="${AIFORGE_NEO4J_URI:-bolt://127.0.0.1:7687}" \
      "$PY" -m aiforge_core.index.graphify_loader \
      --graph "$GRAPH_JSON" --repo "$REPO_NAME" 2>&1 | tail -3
    echo "[graphify_loader] mirrored to Neo4j"
  fi

  # 4) tree-sitter direct AST ingest — covers languages Graphify
  #    doesn't extract semantic edges for, fills :File / :Symbol
  #    when graphify mirror is partial.
  if [ -x "$PY" ]; then
    AIFORGE_NEO4J_URI="${AIFORGE_NEO4J_URI:-bolt://127.0.0.1:7687}" \
      timeout 900 "$PY" -m aiforge_core.index.treesitter_ingest \
      --repo "$REPO_DIR" --repo-name "$REPO_NAME" \
      --languages java python typescript 2>&1 | tail -3 \
      || echo "[treesitter] failed (non-fatal)"
    echo "[treesitter] ingest complete"
  fi

  echo "=== reindex done in $SECONDS s ==="
) >>"$LOG" 2>&1 &

# Detach + return immediately so git hooks don't block the user.
disown
exit 0
