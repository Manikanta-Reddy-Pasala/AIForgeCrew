#!/bin/bash
# Sync laptop's Claude project memory directories to the NUC, then
# ingest each markdown file into AFM via the gap-9 spine so they show
# up in unified_query / memory_block.
#
# KISS: rsync + per-file `aiforge-memory ingest-external` calls.
# No daemon, no API; designed to run from the laptop (or from any
# host with ssh access to the NUC).
#
# Env:
#   AIFORGE_NUC_HOST  ai@192.168.70.115 (default)
#   AIFORGE_CLAUDE_LOCAL  ~/.claude/projects (default)
#   AIFORGE_CLAUDE_REMOTE ~/aiforge-claude-memory-sync (default)
#   AIFORGE_CLAUDE_REPO   default: per-project name derived from dir slug
set -euo pipefail

NUC_HOST="${AIFORGE_NUC_HOST:-ai@192.168.70.115}"
LOCAL_ROOT="${AIFORGE_CLAUDE_LOCAL:-$HOME/.claude/projects}"
REMOTE_ROOT="${AIFORGE_CLAUDE_REMOTE:-aiforge-claude-memory-sync}"

[ -d "$LOCAL_ROOT" ] || { echo "no $LOCAL_ROOT"; exit 1; }

# 1. rsync every project's memory/ dir (only files we care about).
ssh "$NUC_HOST" "mkdir -p \"\$HOME/$REMOTE_ROOT\""
rsync -az --delete \
  --include="*/" --include="memory/" \
  --include="memory/*.md" \
  --exclude="*" \
  "$LOCAL_ROOT/" "$NUC_HOST:\$HOME/$REMOTE_ROOT/"

# 2. Ingest each memory/*.md as a Note_v2 under a per-project AFM repo
#    name (derived from the dir name's last path slug, e.g.
#    "-Users-manip-Documents-codeRepo-AIForgeCrew" → "AIForgeCrew").
ssh "$NUC_HOST" bash -s <<'REMOTE'
set -euo pipefail
ROOT="$HOME/aiforge-claude-memory-sync"
[ -d "$ROOT" ] || exit 0
# Pick the first aiforge-memory venv available — KISS, no install.
CLI=""
for cand in \
  "$HOME/AIForgeCrew/.venv/bin/aiforge-memory" \
  "$HOME/codeRepo/AiForgeMemory/.venv/bin/aiforge-memory" \
  "$(command -v aiforge-memory 2>/dev/null || true)"; do
  if [ -x "$cand" ]; then CLI="$cand"; break; fi
done
if [ -z "$CLI" ]; then
  echo "no aiforge-memory CLI on NUC" >&2
  exit 2
fi
# Source runtime env (Neo4j + LM Studio creds) so the CLI talks to
# the right backends. Missing file → ingest still works against
# defaults (bolt://127.0.0.1:7687 / neo4j / password).
[ -f "$HOME/.aiforge/runtime.env" ] && set -a && \
  source "$HOME/.aiforge/runtime.env" && set +a || true
ok=0; fail=0
for proj_dir in "$ROOT"/*/; do
  [ -d "$proj_dir/memory" ] || continue
  proj_slug="$(basename "$proj_dir")"
  repo="${proj_slug##*-}"
  if [ -z "$repo" ] || [ "$repo" = "$proj_slug" ]; then
    repo="$proj_slug"
  fi
  for md in "$proj_dir/memory"/*.md; do
    [ -f "$md" ] || continue
    if "$CLI" ingest-external "$md" \
        --repo "$repo" \
        --source-type claude_memory \
        --tags "source:claude_memory" >/dev/null 2>&1; then
      ok=$((ok+1))
    else
      fail=$((fail+1))
    fi
  done
done
echo "sync ok ingested=$ok failed=$fail"
REMOTE
