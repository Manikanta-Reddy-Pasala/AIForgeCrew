#!/usr/bin/env bash
# Dispatch Developer on a Paperclip ticket (picks up Thinker's breakdown).
set -uo pipefail
TICKET="${1:?usage: $0 <TICKET-ID>}"
SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
MODEL="qwen3-coder-next"
TURNS=150
remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

echo "=== Developer dispatch for $TICKET ==="

# Reassign ticket to Developer
DEV_ID="e0502e94-0608-4fb9-9afa-b70d8dbf014a"
remote "PGPASSWORD=paperclip \$HOME/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"UPDATE issues SET assignee_agent_id='$DEV_ID' WHERE identifier='$TICKET'\""

# Grab Thinker breakdown
BREAKDOWN=$(remote "PGPASSWORD=paperclip \$HOME/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"SELECT body FROM issue_comments WHERE issue_id = (SELECT id FROM issues WHERE identifier='$TICKET') AND body LIKE '%READY_FOR_DEV%' ORDER BY created_at DESC LIMIT 1\"")

PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<EOF
You are the Developer. Ticket $TICKET is now assigned to you. Your role is defined in ~/.paperclip/.../AGENTS.md (already in your system prompt).

THINKER'S BREAKDOWN (READ CAREFULLY — act only on what it says):

$BREAKDOWN

Branch to use: aiforge/$TICKET (already exists, pushed to origin).

EXECUTE YOUR ROLE per AGENTS.md:
1. cd ~/codeRepo/MongoDbService
2. git fetch origin && git checkout aiforge/$TICKET
3. Implement the "Implementation Plan" sub-steps from the Thinker's breakdown above. No scope creep, no refactoring outside those lines.
4. Commit per sub-step. Commit message format: fix(stock-transfer): $TICKET <short desc>.
5. git push origin aiforge/$TICKET
6. Post comment on ticket $TICKET via Paperclip API:
   a. Get ticket UUID: curl -s 'http://127.0.0.1:3100/api/companies/fd294bd0-2f65-405f-b443-fb41d66226fb/issues?assigneeAgentId=e0502e94-0608-4fb9-9afa-b70d8dbf014a' | python3 -c 'import sys,json;print([x["id"] for x in json.loads(sys.stdin.read()) if x["identifier"]=="$TICKET"][0])'
   b. POST comment: curl -s -X POST http://127.0.0.1:3100/api/issues/<UUID>/comments -H 'Content-Type: application/json' -d '{"body": "<TICKET>.1 committed: <sha>\n\nREADY_FOR_TEST"}'

DO NOT write test files. DO NOT open PR — Tester does it.
If spec is unclear, comment NEEDS_MORE_ANALYSIS and stop — do NOT guess.

Max 150 turns.
EOF

PROMPT_REMOTE="/tmp/developer-$TICKET.txt"
scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$PROMPT_LOCAL" "$SSH_HOST:$PROMPT_REMOTE"
rm -f "$PROMPT_LOCAL"

HERMES_LOG="/tmp/hermes-developer-$TICKET.log"
START=$(date +%s)
echo "  launching Developer hermes..."
remote "~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
END=$(date +%s); WALL=$((END - START))
echo "  wall: ${WALL}s"

echo "---commits on branch---"
remote "cd ~/codeRepo/MongoDbService && git log --oneline origin/aiforge/$TICKET ^master 2>/dev/null | head"
echo "---ticket comments---"
remote "PGPASSWORD=paperclip \$HOME/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"SELECT LEFT(body, 150) FROM issue_comments WHERE issue_id = (SELECT id FROM issues WHERE identifier='$TICKET') ORDER BY created_at DESC LIMIT 3\""
echo "---session stats---"
LATEST=$(remote "ls -t ~/.hermes/sessions/session_202604*.json | head -1")
remote "python3 <<PYEOF
import json
from collections import Counter
s=json.load(open('$LATEST'))
msgs=s.get('messages',[])
tools=[tc.get('function',{}).get('name','?') for m in msgs if m.get('role')=='assistant' and m.get('tool_calls') for tc in m['tool_calls']]
print(f'msgs={len(msgs)} tools={len(tools)}')
print(f'breakdown={dict(Counter(tools))}')
PYEOF"
echo "done"
