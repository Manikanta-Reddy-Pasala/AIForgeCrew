#!/usr/bin/env bash
# scripts/mempalace-wipe-reindex.sh — nuke + re-init + re-mine project palace.
# Use after ChromaDB corruption or to rebuild from scratch.
set -euo pipefail

PROJECT_PALACE="${PROJECT_PALACE:-$HOME/AIForgeCrew/.aiforge/mem/project}"
MEMPALACE="${MEMPALACE:-$HOME/AIForgeCrew/.venv/bin/mempalace}"

[[ -x "$MEMPALACE" ]] || { echo "mempalace missing: $MEMPALACE" >&2; exit 1; }

echo ">>> wiping $PROJECT_PALACE"
rm -rf "$PROJECT_PALACE"
mkdir -p "$PROJECT_PALACE"

echo ">>> init fresh project palace"
"$MEMPALACE" --palace "$PROJECT_PALACE" init "$PROJECT_PALACE" --yes </dev/null

echo ">>> running full index"
bash "$(dirname "$0")/mempalace-index-all.sh"
