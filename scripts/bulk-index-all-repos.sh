#!/usr/bin/env bash
# Seed T4 + Graphify for every git repo under ~/codeRepo (and AIForgeCrew).
# Runs ON Mac Studio. Idempotent: reindex-code clears per-repo tier rows first.
set -euo pipefail

REPO_BASE="${REPO_BASE:-$HOME/codeRepo}"
AIFORGE="${AIFORGE:-$HOME/AIForgeCrew}"
AIFORGE_BIN="${AIFORGE_BIN:-$AIFORGE/.venv/bin/aiforge}"

export PATH="$HOME/.local/bin:$PATH"

# 1. AIForgeCrew uses repo-specific DEFAULT_SOURCES
echo "=== AIForgeCrew (curated sources) ==="
"$AIFORGE_BIN" memory reindex-code --repo aiforge --root "$AIFORGE"
( cd "$AIFORGE" && graphify update . >/dev/null 2>&1 && echo "  graphify: ok" ) || echo "  graphify: skipped"

# 2. Every other git repo under codeRepo gets generic multi-language globs
for dir in "$REPO_BASE"/*; do
  [[ -d "$dir/.git" ]] || continue
  name="$(basename "$dir")"
  [[ "$name" == "AIForgeCrew" ]] && continue

  echo
  echo "=== $name ==="
  "$AIFORGE_BIN" memory reindex-code --repo "$name" --root "$dir" --generic || {
    echo "  T4 reindex FAILED for $name"
    continue
  }

  ( cd "$dir" && graphify update . >/dev/null 2>&1 && echo "  graphify: ok" ) || echo "  graphify: skipped"
done

echo
echo "=== summary ==="
export PGPASSWORD=paperclip
psql -d aiforge -c "SELECT wing, COUNT(*) FROM memories WHERE tier='t4' GROUP BY wing ORDER BY wing"
