#!/usr/bin/env bash
# scripts/hermes-seed-memory.sh — seed Hindsight with existing Claude memory
# (~/.claude/memory + ~/.claude/projects/*/memory + AIForgeCrew project notes).
#
# Uses `hermes memory retain` to push each file as a first-class memory with
# appropriate tags so Hindsight's graph can link entities across roles.
#
# Idempotent — Hindsight dedupes by content hash.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "hermes-seed-memory: macOS only" >&2; exit 1; }

HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
CLAUDE_MEMORY="${CLAUDE_MEMORY:-$HOME/.claude/memory}"
CLAUDE_PROJECTS="${CLAUDE_PROJECTS:-$HOME/.claude/projects}"
REPO_DIR="${REPO_DIR:-$HOME/AIForgeCrew}"

[[ -x "$HERMES_BIN" ]] || { echo "hermes CLI missing" >&2; exit 1; }

retain_file() {
  local f="$1" source_tag="$2" namespace="$3"
  [[ -f "$f" && -s "$f" ]] || return 0

  # Skip files > 100 KB — Hindsight prefers many small memories over few big ones.
  local size
  size=$(wc -c < "$f" | tr -d ' ')
  if [[ "$size" -gt 102400 ]]; then
    echo "  [skip-big ] $f ($size bytes)"
    return 0
  fi

  local title
  title=$(basename "$f")

  "$HERMES_BIN" memory retain \
    --namespace "$namespace" \
    --source "$source_tag" \
    --title "$title" \
    --tag "seed" \
    --tag "$source_tag" \
    --file "$f" 2>/dev/null \
    && echo "  [retain   ] $namespace :: $title" \
    || echo "  [fail     ] $namespace :: $title"
}

count=0
walk() {
  local root="$1" source_tag="$2" namespace="$3"
  [[ -d "$root" ]] || { echo "  [skip-dir ] $root missing"; return 0; }
  while IFS= read -r f; do
    retain_file "$f" "$source_tag" "$namespace"
    count=$((count + 1))
  done < <(find "$root" -type f \( -name "*.md" -o -name "*.txt" -o -name "MEMORY.md" \) 2>/dev/null)
}

echo ">>> seeding Claude global memory ($CLAUDE_MEMORY)"
walk "$CLAUDE_MEMORY" "claude-global" "project/aiforgecrew"

echo ">>> seeding Claude project memory ($CLAUDE_PROJECTS/*/memory)"
for proj_memdir in "$CLAUDE_PROJECTS"/*/memory; do
  [[ -d "$proj_memdir" ]] || continue
  proj_name=$(basename "$(dirname "$proj_memdir")")
  walk "$proj_memdir" "claude-$proj_name" "project/aiforgecrew"
done

echo ">>> seeding AIForgeCrew repo docs"
for d in "$REPO_DIR/docs" "$REPO_DIR/DESIGN.md" "$REPO_DIR/README.md"; do
  if [[ -d "$d" ]]; then
    walk "$d" "aiforge-docs" "project/aiforgecrew"
  elif [[ -f "$d" ]]; then
    retain_file "$d" "aiforge-docs" "project/aiforgecrew"
    count=$((count + 1))
  fi
done

echo
echo "=== seeded $count memory files ==="
echo
echo "Verify with:"
echo "  hermes memory recall \"DESIGN.md permission matrix\" --top 5"
echo "  hermes memory stats"
