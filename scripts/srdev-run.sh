#!/usr/bin/env bash
# Dispatch Sr Dev on a ticket. Disables Paperclip auto-retry during run, posts verdict ourselves.
set -uo pipefail

TICKET="${1:?usage: $0 <TICKET-ID>}"
SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
MODEL="gemma-4-31b-it"
TURNS=150
SRDEV_ID="28b8c064-bfcf-44e1-9e91-e37c39e0097c"
CID="fd294bd0-2f65-405f-b443-fb41d66226fb"

remote() { ssh -o IdentitiesOnly=yes -o ServerAliveInterval=60 -o ServerAliveCountMax=30 -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }
rpsql() { remote "PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"$1\""; }
pc_comment() {
  local issue_uuid="$1" body="$2"
  local json=$(printf '%s' "$body" | python3 -c 'import sys,json; print(json.dumps({"body": sys.stdin.read()}))')
  echo "$json" | remote "curl -sS -X POST 'http://localhost:3100/api/issues/$issue_uuid/comments' -H 'Content-Type: application/json' --data @-" >/dev/null
}

bash "$(dirname "$0")/lib/ensure-model.sh" gemma-4-31b-it 65536 || { echo "ensure-model failed"; exit 1; }

echo "=== Sr Dev: $TICKET ==="

# Local ticket context must exist
LOCAL_CTX="docs/tickets/$TICKET.md"
[[ ! -f "$LOCAL_CTX" ]] && { echo "ERROR: $LOCAL_CTX missing"; exit 1; }

# Sync to remote
scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$LOCAL_CTX" "$SSH_HOST:~/AIForgeCrew/docs/tickets/$TICKET.md"

# Disable Paperclip auto-retry: set ticket to 'backlog' + ensure assignee set
ISSUE_UUID=$(rpsql "SELECT id FROM issues WHERE identifier='$TICKET'")
[[ -z "$ISSUE_UUID" ]] && { echo "ERROR: ticket $TICKET not found"; exit 1; }
rpsql "UPDATE issues SET assignee_agent_id='$SRDEV_ID', status='backlog' WHERE identifier='$TICKET'" >/dev/null
echo "  ticket set to backlog (paperclip won't auto-retry)"

PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<EOF
You are Sr Developer. Ticket $TICKET.

MANDATORY FIRST STEPS (do NOT skip):
1. cat /Users/manikanta/AIForgeCrew/docs/tickets/$TICKET.md   # Architect context
2. rag "<topic from ticket>" (1-3 queries max)
3. hindsight_recall × 3

Then:
4. For each involved repo in context: cd ~/codeRepo/<repo> && git fetch origin && git checkout aiforge/$TICKET
5. Read ≤3 anchor files
6. Write /Users/manikanta/AIForgeCrew/docs/breakdowns/$TICKET.md with numbered sub-tasks (each ≤15 min Developer work, each names test case)
7. cd ~/AIForgeCrew && git add docs/breakdowns/$TICKET.md && git commit -m "docs: breakdown for $TICKET" && git push

DO NOT commit breakdown in any other repo (keep it in AIForgeCrew only).
DO NOT write src/ code. DO NOT write test files.

You may call the Paperclip comment API at the end, but dispatcher will also post a summary automatically — focus on the breakdown file.

Max $TURNS turns.
EOF

PROMPT_REMOTE="/tmp/srdev-$TICKET.txt"
scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$PROMPT_LOCAL" "$SSH_HOST:$PROMPT_REMOTE"
rm -f "$PROMPT_LOCAL"

HERMES_LOG="/tmp/hermes-srdev-$TICKET.log"
START=$(date +%s)
echo "launching Sr Dev hermes (gemma-4-31b-it, max-turns=$TURNS)..."
remote "~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
END=$(date +%s); WALL=$((END - START))
echo "wall: ${WALL}s"

# Pull breakdown back
if remote "test -f ~/AIForgeCrew/docs/breakdowns/$TICKET.md"; then
  scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST:~/AIForgeCrew/docs/breakdowns/$TICKET.md" "docs/breakdowns/$TICKET.md"
  LINES=$(wc -l < "docs/breakdowns/$TICKET.md")
  echo "breakdown fetched: docs/breakdowns/$TICKET.md ($LINES lines)"
  BREAKDOWN_SUMMARY=$(head -40 "docs/breakdowns/$TICKET.md")
  pc_comment "$ISSUE_UUID" "$(printf 'SR DEV DONE — wall=%ss\n\n%s\n\nFull breakdown: docs/breakdowns/%s.md\n\nREADY_FOR_DEV' "$WALL" "$BREAKDOWN_SUMMARY" "$TICKET")"
  echo "  posted READY_FOR_DEV comment to Paperclip"
  rpsql "UPDATE issues SET status='todo' WHERE identifier='$TICKET'" >/dev/null
else
  echo "NO BREAKDOWN FILE — check $HERMES_LOG"
  pc_comment "$ISSUE_UUID" "SR DEV FAILED — no breakdown file produced after $WALL s. Check $HERMES_LOG. NEEDS_HUMAN"
fi

echo "---session stats---"
LATEST=$(remote "ls -t ~/.hermes/sessions/session_202604*.json | head -1")
remote "python3 - <<PYEOF
import json
from collections import Counter
s=json.load(open('$LATEST'))
msgs=s.get('messages',[])
tools=[tc.get('function',{}).get('name','?') for m in msgs if m.get('role')=='assistant' and m.get('tool_calls') for tc in m['tool_calls']]
print(f'msgs={len(msgs)} tools={len(tools)}')
print(f'breakdown={dict(Counter(tools))}')
PYEOF"
echo "done"
