#!/usr/bin/env bash
# Install a post-commit hook that incrementally refreshes the Graphify code
# knowledge graph and the T4 memory store after every commit. Idempotent.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO_ROOT/.git/hooks/post-commit"

cat > "$HOOK" <<'SH'
#!/usr/bin/env bash
# Auto-refresh Graphify + T4 memory after each commit. Non-blocking.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
LOG_DIR="$HOME/.aiforge/logs"
mkdir -p "$LOG_DIR"

# Only fire on the main checkout, not while rebasing / detached-HEAD.
branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
[[ -z "$branch" ]] && exit 0

export PATH="$HOME/.local/bin:$PATH"

(
  cd "$REPO_ROOT"
  # Graphify: AST-only incremental
  if command -v graphify >/dev/null; then
    graphify update . >>"$LOG_DIR/post-commit-graphify.log" 2>&1 || true
  fi
  # T4 reindex — only if aiforge CLI + pg reachable
  if [[ -x .venv/bin/aiforge ]]; then
    .venv/bin/aiforge memory reindex-code --repo aiforge --root . \
      >>"$LOG_DIR/post-commit-reindex.log" 2>&1 || true
  fi
) &

disown
SH

chmod +x "$HOOK"
echo "post-commit hook installed at $HOOK"
