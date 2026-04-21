#!/usr/bin/env bash
# Provision the runtime on Mac Studio. Idempotent.
#
# Steps:
#   1. Ensure aiforge Postgres schema (tickets tables) via migration SQL.
#   2. Install openai + psycopg into AIForgeCrew .venv.
#   3. Load LM Studio models at max context with 8h TTL.
#   4. Backfill embeddings for memories rows missing them.
#   5. Install launchd plists.
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

echo ">>> 1/5 applying aiforge schema"
"$PSQL" -h 127.0.0.1 -U manikanta aiforge -f "$REPO/db/migrations/2026-04-21-tickets.sql"

echo ">>> 2/5 installing python deps (via uv)"
cd "$REPO" && /opt/homebrew/bin/uv pip install --python "$VENV/bin/python" \
  openai 'psycopg[binary]' pgvector 2>&1 | tail -4

echo ">>> 3/5 loading models (ONLY hot roles pre-loaded; tiny roles JIT on first call)"
"$LMS" unload --all 2>&1 | tail -1
# Hot: Planner + Doer are most tick traffic — keep loaded with 8h TTL.
"$LMS" load qwen3.6-35b-a3b                 --context-length 65536  --ttl 28800 --yes 2>&1 | tail -1
"$LMS" load qwen3-coder-next                 --context-length 131072 --ttl 28800 --yes 2>&1 | tail -1
# Supervisor / Feedback / Learner are NOT pre-loaded — RAM pressure would
# exceed 90 GB budget. LM Studio auto-loads them on first inference call
# via OpenAI-compat API (JIT). TTL defaults from LM Studio settings; if
# you want to override per-model, use `lms load <id> --ttl 1800` ad-hoc.
echo "Note: supervisor/feedback/learner models JIT-load on first tick."
"$LMS" ps

echo ">>> 4/5 backfilling embeddings (idempotent)"
"$VENV/bin/python" "$REPO/scripts/runtime/embed-backfill.py" --limit 5000

echo ">>> 5/5 installing launchd plists"
bash "$REPO/scripts/runtime/install-launchd.sh"

echo
echo "install complete. First tick fires within 60s. Watch:"
echo "  tail -f ~/.aiforge/logs/orchestrator-*.ndjson | jq -c '{ts,role,ticket,event,tool,dur_ms}'"
