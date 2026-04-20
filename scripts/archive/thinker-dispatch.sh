#!/usr/bin/env bash
# Dispatch Sr Dev (Thinker) on a Paperclip ticket.
# Direct hermes invocation with Thinker AGENTS.md + ticket body in prompt.
set -uo pipefail

TICKET="${1:?usage: $0 <TICKET-ID>}"
SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
MODEL="gemma-4-31b-it"
TURNS=150
remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

echo "=== Thinker dispatch for $TICKET ==="

# Fetch ticket body
BODY=$(remote "curl -s 'http://localhost:3100/api/issues/?identifier=$TICKET' 2>/dev/null; PGPASSWORD=paperclip \$HOME/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"SELECT title || E'\\\n---\\\n' || description FROM issues WHERE identifier='$TICKET'\"")
echo "  ticket body: $(echo "$BODY" | head -3)"

PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<EOF
You are Sr Dev (Thinker). Ticket $TICKET is assigned to you. Your instructions are in ~/.paperclip/.../instructions/AGENTS.md (already loaded in your system prompt).

TICKET BODY:
$BODY

EXECUTE YOUR ROLE as defined in your AGENTS.md:
1. hindsight_recall × 3 (topic-relevant)
2. rag CLI × up to 5 (use \`rag "<query>"\` via terminal tool)
3. Read ≤3 anchor files identified from retrieval
4. Write breakdown comment on ticket $TICKET via Paperclip API:
   curl -s -X POST http://127.0.0.1:3100/api/issues/<ISSUE-UUID>/comments -H 'Content-Type: application/json' -d '{"body":"..."}'
   Get ISSUE-UUID via: curl -s 'http://127.0.0.1:3100/api/companies/fd294bd0-2f65-405f-b443-fb41d66226fb/issues?identifier=$TICKET' | jq -r '.[0].id' (or use python3 json)
5. Create branch aiforge/$TICKET in MongoDbService (and any other involved repo):
   cd ~/codeRepo/MongoDbService && git checkout master && git pull && git checkout -b aiforge/$TICKET && git push -u origin aiforge/$TICKET
6. End comment with literal marker "READY_FOR_DEV" on its own line.

NO CODE CHANGES. NO TEST FILES. Only the comment + branch creation.
Max 150 turns. Target ~15 min.
EOF

PROMPT_REMOTE="/tmp/thinker-$TICKET.txt"
scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$PROMPT_LOCAL" "$SSH_HOST:$PROMPT_REMOTE"
rm -f "$PROMPT_LOCAL"

HERMES_LOG="/tmp/hermes-thinker-$TICKET.log"
START=$(date +%s)
echo "  launching Thinker hermes..."
remote "~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
END=$(date +%s); WALL=$((END - START))
echo "  wall: ${WALL}s"

# Check outcomes
echo "---branch check---"
remote "cd ~/codeRepo/MongoDbService && git branch -a | grep 'aiforge/$TICKET' | head"
echo "---ticket comments---"
remote "PGPASSWORD=paperclip \$HOME/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"SELECT LEFT(body, 200) FROM issue_comments WHERE issue_id = (SELECT id FROM issues WHERE identifier='$TICKET') ORDER BY created_at DESC LIMIT 3\""
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
