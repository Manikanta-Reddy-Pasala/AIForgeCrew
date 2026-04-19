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

MEMPALACE="${MEMPALACE:-$HOME/AIForgeCrew/.venv/bin/mempalace}"
if [[ -x "$MEMPALACE" ]]; then
  PATH="$(dirname "$MEMPALACE"):$PATH"
else
  command -v mempalace >/dev/null || { echo "mempalace missing: run `make mempalace-install` first" >&2; exit 1; }
fi
[[ -d "$PROJECT_PALACE" ]] || mempalace --palace "$PROJECT_PALACE" init "$PROJECT_PALACE" --yes </dev/null

# --- codeRepo: mine each subrepo as its own wing ---
# NB: `--mode projects` (plural) for code/docs, `--mode convos` for chat logs.
if [[ -d "$CODE_DIR" ]]; then
  echo ">>> mining codeRepo/ into project palace (wing per subdir)"
  for d in "$CODE_DIR"/*/; do
    [[ -d "$d" ]] || continue
    wing=$(basename "$d")
    echo "  mine: $wing"
    "$MEMPALACE" --palace "$PROJECT_PALACE" mine "$d" --wing "$wing" --mode projects 2>&1 | tail -1 \
      || echo "  WARN: mining $wing failed (continuing)"
  done
else
  echo "[skip] $CODE_DIR (missing — run scripts/sync-code-repos.sh first)"
fi

# --- Claude daily memory notes ---
if [[ -d "$CLAUDE_MEMORY" ]]; then
  echo ">>> mining Claude daily memory"
  "$MEMPALACE" --palace "$PROJECT_PALACE" mine "$CLAUDE_MEMORY" --wing claude-daily --mode convos 2>&1 | tail -1 || true
fi

# --- claude-mem observer sessions (if present) ---
OBS="$HOME/.claude/projects/-Users-manip--claude-mem-observer-sessions"
if [[ -d "$OBS" ]]; then
  echo ">>> mining claude-mem observer sessions"
  "$MEMPALACE" --palace "$PROJECT_PALACE" mine "$OBS" --wing claude-observer --mode convos 2>&1 | tail -1 || true
fi

echo
echo "=== palace status ==="
mempalace --palace "$PROJECT_PALACE" status | head -40
