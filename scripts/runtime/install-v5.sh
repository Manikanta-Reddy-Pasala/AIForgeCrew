#!/usr/bin/env bash
# Provision the v5 runtime on Mac Studio. Idempotent. Non-destructive (use
# cleanup-v4.sh separately to retire legacy services).
#
# Steps:
#   1. Ensure aiforge Postgres schema (tickets tables) via migration SQL.
#   2. Install openai + psycopg into AIForgeCrew .venv.
#   3. Load LM Studio models at max context with 8h TTL.
#   4. Run hindsight → aiforge migration (once, skips if already done).
#   5. Backfill embeddings for new memories rows.
#   6. Install launchd plists.
#
# Usage (on Mac Studio):
#   bash scripts/runtime/install-v5.sh
set -euo pipefail

REPO="${REPO:-$HOME/AIForgeCrew}"
VENV="$REPO/.venv"
PSQL=/Users/manikanta/.pg0/installation/18.1.0/bin/psql
LMS=$HOME/.lmstudio/bin/lms

[[ -d "$REPO" ]] || { echo "no $REPO — git pull first" >&2; exit 1; }
[[ -x "$VENV/bin/python" ]] || { echo "no $VENV — run scripts/install-aiforge.sh first" >&2; exit 1; }

echo ">>> 1/6 applying aiforge schema"
"$PSQL" -h 127.0.0.1 -U manikanta aiforge -f "$REPO/db/migrations/2026-04-21-tickets.sql"

echo ">>> 2/6 installing python deps (via uv)"
cd "$REPO" && /opt/homebrew/bin/uv pip install --python "$VENV/bin/python" \
  openai 'psycopg[binary]' pgvector 2>&1 | tail -4

echo ">>> 3/6 loading models at max context"
"$LMS" unload --all 2>&1 | tail -1
"$LMS" load qwen3.6-35b-a3b                 --context-length 131072 --ttl 28800 --yes 2>&1 | tail -1
"$LMS" load qwen3-coder-next                 --context-length 262144 --ttl 28800 --yes 2>&1 | tail -1
"$LMS" load qwen/qwen3-4b-thinking-2507      --context-length 131072 --ttl 28800 --yes 2>&1 | tail -1
"$LMS" ps

echo ">>> 4/6 migrating hindsight → aiforge.memories (idempotent)"
bash "$REPO/scripts/runtime/migrate-hindsight-to-aiforge.sh" || true

echo ">>> 5/6 backfilling embeddings"
"$VENV/bin/python" "$REPO/scripts/runtime/embed-backfill.py" --limit 5000

echo ">>> 6/6 installing launchd plists"
bash "$REPO/scripts/runtime/install-v5-launchd.sh"

echo
echo "v5 install complete. First tick fires within 60s. Watch:"
echo "  tail -f ~/.aiforge/logs/orchestrator-*.ndjson | jq -c '{ts,role,ticket,event,tool,dur_ms}'"
