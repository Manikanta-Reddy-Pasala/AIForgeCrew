#!/usr/bin/env bash
# scripts/paperclip-em-use-claude.sh — switch EM agent to Paperclip's claude_local adapter.
#
# Paperclip's claude_local adapter spawns the real Claude Code CLI per heartbeat,
# using the user's Claude.ai subscription. No API key — `claude /login` already
# done means the subscription token lives at ~/.claude/ on the Mac Studio.
#
# Runs on Mac Studio. Idempotent: safe to re-run.
set -euo pipefail

BASE="${PAPERCLIP_BASE:-http://localhost:3100}"
COMPANY_NAME="${COMPANY_NAME:-OneShell}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-7}"

command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

COMPANIES=$(curl -sS "$BASE/api/companies")
CID=$(echo "$COMPANIES" | jq -r --arg n "$COMPANY_NAME" '[.[] | select(.name==$n)][0].id // empty')
[[ -n "$CID" ]] || { echo "company '$COMPANY_NAME' not found" >&2; exit 1; }

EM_ID=$(curl -sS "$BASE/api/companies/$CID/agents" \
  | jq -r '[.[] | select(.role=="pm" and .name=="Engineering Manager")][0].id // empty')
[[ -n "$EM_ID" ]] || { echo "EM agent not found" >&2; exit 1; }

echo ">>> switching EM (id=$EM_ID) → adapterType=claude_local, model=$CLAUDE_MODEL"
body=$(jq -nc --arg m "$CLAUDE_MODEL" '{
  adapterType: "claude_local",
  adapterConfig: {
    model: $m
  },
  runtimeConfig: {
    heartbeat: {enabled: true, intervalSec: 60}
  }
}')
curl -sS -X PATCH "$BASE/api/agents/$EM_ID" -H 'Content-Type: application/json' -d "$body" \
  | jq '{id, name, adapterType, adapterConfig}'

echo
echo "EM now uses Claude Code CLI (claude_local adapter)."
echo "Test with:  curl -s $BASE/api/companies/$CID/adapters/claude_local/models | jq ."
