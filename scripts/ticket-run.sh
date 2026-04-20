#!/usr/bin/env bash
# Full pipeline orchestrator: srdev → dev → review → (loop) bounce → review, max N bounces.
set -uo pipefail

TICKET="${1:?usage: $0 <TICKET-ID>}"
MAX_BOUNCES="${MAX_BOUNCES:-2}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

echo "╔══════════════════════════════════════════════════╗"
echo "║  Full pipeline: $TICKET"
echo "║  Max bounces: $MAX_BOUNCES"
echo "╚══════════════════════════════════════════════════╝"

# Phase 1: Sr Dev breakdown
[[ -f "docs/breakdowns/$TICKET.md" ]] || {
  echo ">>> [1/N] Sr Dev — writing breakdown"
  bash "$SCRIPT_DIR/srdev-run.sh" "$TICKET"
}

# Phase 2: Developer first pass
echo ">>> [2/N] Developer — initial implementation"
bash "$SCRIPT_DIR/dev-run.sh" "$TICKET"

# Phase 3: Review loop
round=0
while (( round < MAX_BOUNCES + 1 )); do
  echo ">>> [3+round$round] Review"
  bash "$SCRIPT_DIR/review-run.sh" "$TICKET"

  # Check latest review verdict
  REVIEW_STATUS=$(ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 manikanta@192.168.70.185 \
    "PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"SELECT body FROM issue_comments WHERE issue_id=(SELECT id FROM issues WHERE identifier='$TICKET') AND body LIKE '%VERDICT_START%' ORDER BY created_at DESC LIMIT 1\"" 2>/dev/null || echo "")
  if echo "$REVIEW_STATUS" | grep -q "status: READY_FOR_REVIEW"; then
    echo "✅ PIPELINE COMPLETE — $TICKET ready for human review"
    break
  fi
  if ! echo "$REVIEW_STATUS" | grep -q "NEEDS_DEV_REWORK"; then
    echo "⚠️ unknown review verdict — stopping"
    break
  fi

  # Needs rework
  round=$((round + 1))
  if (( round > MAX_BOUNCES )); then
    echo "❌ PIPELINE STOPPED — exceeded $MAX_BOUNCES bounces. NEEDS_HUMAN."
    break
  fi
  echo ">>> [4+round$round] Bounce — Developer rework"
  MAX_BOUNCES="$MAX_BOUNCES" bash "$SCRIPT_DIR/bounce-run.sh" "$TICKET"
done

echo ">>> pipeline summary:"
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 manikanta@192.168.70.185 \
  "PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"SELECT identifier, status, assignee_agent_id FROM issues WHERE identifier='$TICKET'\""
