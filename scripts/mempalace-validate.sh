#!/usr/bin/env bash
# scripts/mempalace-validate.sh — verify MemPalace has the laptop memory +
# codeRepo content, and that search actually returns hits.
#
# Runs on Mac Studio. Reports:
#   - palace dirs + sizes
#   - `mempalace status` summary (rooms/wings/drawers per palace)
#   - sample searches across codebase + claude-memory wings
#   - whether Hermes skill pack references aiforge-memory skill
set -euo pipefail

PROJECT_PALACE="${PROJECT_PALACE:-$HOME/AIForgeCrew/.aiforge/mem/project}"
MEMPALACE="${MEMPALACE:-$HOME/AIForgeCrew/.venv/bin/mempalace}"

command -v "$MEMPALACE" >/dev/null || { echo "mempalace missing: $MEMPALACE" >&2; exit 1; }
[[ -d "$PROJECT_PALACE" ]] || { echo "project palace missing: $PROJECT_PALACE" >&2; exit 1; }

echo "================================================="
echo "1. Palace inventory"
echo "================================================="
du -sh "$HOME/AIForgeCrew/.aiforge/mem/"* 2>/dev/null || true
echo

echo "================================================="
echo "2. mempalace status (project palace)"
echo "================================================="
"$MEMPALACE" --palace "$PROJECT_PALACE" status 2>&1 | head -80
echo

echo "================================================="
echo "3. Wings present"
echo "================================================="
# Wings = top-level dirs under palace (per MemPalace convention)
ls "$PROJECT_PALACE/wings" 2>/dev/null | head -60 || echo "(no wings dir — check palace layout)"
find "$PROJECT_PALACE" -maxdepth 3 -type d -name "wings" 2>/dev/null | head
echo

echo "================================================="
echo "4. Sample searches"
echo "================================================="

queries=(
  "permission matrix DESIGN"
  "TDD tester coverage"
  "claude memory daily notes"
  "GLM Qwen LM Studio routing"
  "validate email helper"
)
for q in "${queries[@]}"; do
  echo "---- \"$q\" ----"
  "$MEMPALACE" --palace "$PROJECT_PALACE" search "$q" --limit 3 2>&1 | head -20
  echo
done

echo "================================================="
echo "5. Hermes skill pack"
echo "================================================="
ls ~/.hermes/skills/aiforge/ 2>&1 | head
if [[ -f ~/.hermes/skills/aiforge/memory/SKILL.md ]]; then
  echo "  ✓ aiforge-memory skill installed"
  head -10 ~/.hermes/skills/aiforge/memory/SKILL.md
else
  echo "  ✗ aiforge-memory skill missing"
fi
echo

echo "================================================="
echo "6. Per-agent palaces"
echo "================================================="
for role in em tester sr-developer sr-architect; do
  p="$HOME/AIForgeCrew/.aiforge/mem/agent/$role"
  if [[ -d "$p" ]]; then
    size=$(du -sh "$p" | cut -f1)
    echo "  $role: $p ($size)"
  else
    echo "  $role: (no palace dir)"
  fi
done
