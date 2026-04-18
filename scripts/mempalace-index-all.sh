#!/usr/bin/env bash
# scripts/mempalace-index-all.sh — index codeRepo/ + Claude memory into MemPalace.
# Runs on the Mac Studio. Uses the existing .aiforge/mem/ palaces.
#
# Structure:
#   project palace   ← receives everything (shared read for all agents)
#   agent/<role>     ← untouched by this script (per-role private memory)
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "mempalace-index-all: macOS only" >&2; exit 1
fi

PROJECT_PALACE="${PROJECT_PALACE:-$HOME/AIForgeCrew/.aiforge/mem/project}"
CODE_DIR="${CODE_DIR:-$HOME/codeRepo}"
CLAUDE_MEMORY="${CLAUDE_MEMORY:-$HOME/.claude/memory}"
CLAUDE_MEM_DB="${CLAUDE_MEM_DB:-$HOME/.claude-mem/claude-mem.db}"

command -v mempalace >/dev/null || { echo "mempalace missing: pip install mempalace" >&2; exit 1; }
[[ -d "$PROJECT_PALACE" ]] || mempalace --palace "$PROJECT_PALACE" init "$PROJECT_PALACE" --yes </dev/null

# --- codeRepo: mine each subrepo as its own wing ---
if [[ -d "$CODE_DIR" ]]; then
  echo ">>> mining codeRepo/ into project palace (wing per subdir)"
  for d in "$CODE_DIR"/*/; do
    [[ -d "$d" ]] || continue
    wing=$(basename "$d")
    echo "  mine: $wing"
    mempalace --palace "$PROJECT_PALACE" mine "$d" --wing "$wing" --mode project || echo "  WARN: mining $wing failed (continuing)"
  done
else
  echo "[skip] $CODE_DIR (missing — run scripts/sync-code-repos.sh first)"
fi

# --- Claude daily memory notes ---
if [[ -d "$CLAUDE_MEMORY" ]]; then
  echo ">>> mining Claude daily memory"
  mempalace --palace "$PROJECT_PALACE" mine "$CLAUDE_MEMORY" --wing claude-daily --mode convos || true
fi

# --- claude-mem observer sessions (if present) ---
OBS="$HOME/.claude/projects/-Users-manip--claude-mem-observer-sessions"
if [[ -d "$OBS" ]]; then
  echo ">>> mining claude-mem observer sessions"
  mempalace --palace "$PROJECT_PALACE" mine "$OBS" --wing claude-observer --mode convos || true
fi

echo
echo "=== palace status ==="
mempalace --palace "$PROJECT_PALACE" status | head -40
