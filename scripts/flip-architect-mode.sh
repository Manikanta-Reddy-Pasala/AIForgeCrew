#!/usr/bin/env bash
# Flip Paperclip's Architect agent between cloud (Claude) and local_30b (gemma).
# Usage:
#   bash scripts/flip-architect-mode.sh cloud     # Claude Code via claude_local adapter
#   bash scripts/flip-architect-mode.sh local_30b # gemma-4-31b-it via hermes_local
set -euo pipefail

MODE="${1:-}"
[[ "$MODE" == "cloud" || "$MODE" == "local_30b" ]] || {
  echo "usage: $0 {cloud|local_30b}" >&2
  exit 1
}

ARCHITECT_ID="${ARCHITECT_ID:-35760e2f-4cef-4013-9aff-d93592b5f71e}"
export PGPASSWORD=paperclip
PSQL=/Users/manikanta/.pg0/installation/18.1.0/bin/psql
pg() { "$PSQL" -h 127.0.0.1 -p 54329 -U paperclip -d paperclip "$@"; }

case "$MODE" in
  cloud)
    pg -c "
    UPDATE agents SET
      adapter_type='claude_local',
      adapter_config = adapter_config ||
        '{\"model\":\"claude-opus-4-7\",\"provider\":\"claude-cloud\"}'::jsonb
    WHERE id='$ARCHITECT_ID';
    "
    echo "Architect → cloud (Claude Opus 4.7)"
    ;;
  local_30b)
    pg -c "
    UPDATE agents SET
      adapter_type='hermes_local',
      adapter_config = adapter_config ||
        '{\"model\":\"gemma-4-31b-it\",\"provider\":\"auto\",\"hermesCommand\":\"/Users/manikanta/.local/bin/hermes-serial\",\"timeoutSec\":1500}'::jsonb
    WHERE id='$ARCHITECT_ID';
    "
    # Also refresh instructions to the local-30b-specific prompt
    REPO="${REPO:-$HOME/AIForgeCrew}"
    CID="${CID:-fd294bd0-2f65-405f-b443-fb41d66226fb}"
    DST="$HOME/.paperclip/instances/default/companies/$CID/agents/$ARCHITECT_ID/instructions/AGENTS.md"
    if [[ -f "$REPO/agents/architect/system-prompt.local-30b.md" ]]; then
      cp "$REPO/agents/architect/system-prompt.local-30b.md" "$DST"
      echo "  instructions → system-prompt.local-30b.md"
    fi
    echo "Architect → local_30b (gemma-4-31b-it)"
    ;;
esac

pg -Atc "SELECT adapter_type, adapter_config->>'model' FROM agents WHERE id='$ARCHITECT_ID'"
