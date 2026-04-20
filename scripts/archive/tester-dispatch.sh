#!/usr/bin/env bash
# Dispatch Tester on a Paperclip ticket.
set -uo pipefail
TICKET="${1:?usage: $0 <TICKET-ID>}"
SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
MODEL="mistralai/devstral-small-2-2512"
TURNS=150
remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

echo "=== Tester dispatch for $TICKET ==="
TESTER_ID="eb1c388d-8601-4df4-89d8-447ec2ff5946"
remote "PGPASSWORD=paperclip \$HOME/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"UPDATE issues SET assignee_agent_id='$TESTER_ID' WHERE identifier='$TICKET'\""

BREAKDOWN=$(remote "PGPASSWORD=paperclip \$HOME/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"SELECT body FROM issue_comments WHERE issue_id = (SELECT id FROM issues WHERE identifier='$TICKET') AND body LIKE '%READY_FOR_DEV%' ORDER BY created_at DESC LIMIT 1\"")

COMMITS=$(remote "cd ~/codeRepo/MongoDbService && git log --oneline aiforge/$TICKET ^master 2>/dev/null")

PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<EOF
You are Tester. Ticket $TICKET. Role defined in your AGENTS.md (in system prompt).

THINKER'S BREAKDOWN + TEST SPEC:

$BREAKDOWN

DEVELOPER'S COMMITS ON BRANCH aiforge/$TICKET:

$COMMITS

Your job: write unit tests per Thinker's Test Spec. Verify Developer's implementation. If tests expose a bug in Developer's code → bounce with NEEDS_DEV_REWORK. If green → open PR + READY_FOR_REVIEW.

EXECUTE:
1. cd ~/codeRepo/MongoDbService
2. git fetch origin && git checkout aiforge/$TICKET
3. rag "ProductServiceImplTest existing tests" (1 query, find conventions)
4. Write unit tests matching Thinker's spec. Put in src/test/java/com/oneshell/mongodb/feature/product/ProductServiceImplOne51Test.java (or extend existing test class).
5. Run: ./mvnw test -Dtest=ProductServiceImplOne51* -pl . -o 2>&1 | tail -20  (no JDK on this host — compile-check only is fine; report what you see)
6. git add -A && git commit -m "test(stock-transfer): ONE-51 verify single-update atomicity"
7. git push origin aiforge/$TICKET
8. Comment on ticket via Paperclip API:
   a. Get UUID: curl -s 'http://127.0.0.1:3100/api/companies/fd294bd0-2f65-405f-b443-fb41d66226fb/issues' | python3 -c 'import sys,json;print([x["id"] for x in json.loads(sys.stdin.read()) if x["identifier"]=="$TICKET"][0])'
   b. Comment with test results + PR link (if gh available) OR note the test file + commit sha
   c. End with READY_FOR_REVIEW (tests green) OR NEEDS_DEV_REWORK (test failure traces to impl bug)

HARD RULES:
- DO NOT modify src/main code. Only src/test.
- DO NOT write >3 new tests (keep scope tight per spec).
- If spec is unclear, comment NEEDS_CLEARER_SPEC.

Max 150 turns.
EOF

PROMPT_REMOTE="/tmp/tester-$TICKET.txt"
scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$PROMPT_LOCAL" "$SSH_HOST:$PROMPT_REMOTE"
rm -f "$PROMPT_LOCAL"

HERMES_LOG="/tmp/hermes-tester-$TICKET.log"
START=$(date +%s)
echo "  launching Tester hermes..."
remote "~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
END=$(date +%s); WALL=$((END - START))
echo "  wall: ${WALL}s"

echo "---test commits---"
remote "cd ~/codeRepo/MongoDbService && git log --oneline aiforge/$TICKET ^master | head"
echo "---test files---"
remote "cd ~/codeRepo/MongoDbService && git ls-tree -r aiforge/$TICKET --name-only | grep -E 'test.*One?51|OneFiftyOne' | head"
echo "---comments---"
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
