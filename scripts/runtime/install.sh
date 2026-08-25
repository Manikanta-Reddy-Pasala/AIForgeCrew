#!/usr/bin/env bash
# Provision the runtime on Mac Studio. Idempotent.
#
# Steps:
#   1. Ensure aiforge Postgres schema (tickets tables) via migration SQL.
#   2. Install openai + psycopg into AIForgeCrew .venv.
#   3. Load LM Studio models at max context with 8h TTL.
#   4. Install launchd plists.
#
# Usage (on Mac Studio):
#   bash scripts/runtime/install.sh
set -euo pipefail

REPO="${REPO:-$HOME/AIForgeCrew}"
VENV="$REPO/.venv"
PSQL=/Users/manikanta/.pg0/installation/18.1.0/bin/psql
LMS=$HOME/.lmstudio/bin/lms

[[ -d "$REPO" ]] || { echo "no $REPO — git pull first" >&2; exit 1; }
[[ -x "$VENV/bin/python" ]] || { echo "no $VENV — run scripts/install-aiforge.sh first" >&2; exit 1; }

echo ">>> 1/4 schema bootstrap is in-process (aiforge_core.tickets.store._ensure_schema)"
echo "    no migration file applied — first connection from aiforge-api creates tables."

echo ">>> 2/4 installing python deps (via uv)"
cd "$REPO" && /opt/homebrew/bin/uv pip install --python "$VENV/bin/python" \
  openai 'psycopg[binary]' pgvector 2>&1 | tail -4

echo ">>> 3/4 loading Doer model (always hot); Planner loads on-demand via memguard"
"$LMS" unload --all 2>&1 | tail -1
"$LMS" load qwen3-coder-next --context-length 131072 --ttl 28800 --parallel 4 --yes 2>&1 | tail -1
"$LMS" ps

echo ">>> 4/4 installing launchd plists"
bash "$REPO/scripts/runtime/install-launchd.sh"

echo
echo "install complete. First tick fires within 60s. Watch:"
echo "  tail -f ~/.aiforge/logs/orchestrator-*.ndjson | jq -c '{ts,role,ticket,event,tool,dur_ms}'"
