#!/usr/bin/env bash
# Dispatch Sr Dev in REVIEW mode. Disable Paperclip retry. Extract verdict from log + post ourselves.
set -uo pipefail

TICKET="${1:?usage: $0 <TICKET-ID>}"
SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
MODEL="gemma-4-31b-it"
TURNS=120
SRDEV_ID="28b8c064-bfcf-44e1-9e91-e37c39e0097c"

remote() { ssh -o IdentitiesOnly=yes -o ServerAliveInterval=60 -o ServerAliveCountMax=30 -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }
rpsql() { remote "PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"$1\""; }
pc_comment() {
  local uuid="$1" body="$2"
  local json=$(printf '%s' "$body" | python3 -c 'import sys,json; print(json.dumps({"body": sys.stdin.read()}))')
  echo "$json" | remote "curl -sS -X POST 'http://localhost:3100/api/issues/$uuid/comments' -H 'Content-Type: application/json' --data @-" >/dev/null
}

echo "=== Sr Dev REVIEW: $TICKET ==="

ISSUE_UUID=$(rpsql "SELECT id FROM issues WHERE identifier='$TICKET'")
rpsql "UPDATE issues SET assignee_agent_id='$SRDEV_ID', status='backlog' WHERE identifier='$TICKET'" >/dev/null

COMMITS=$(remote "cd ~/codeRepo/MongoDbService 2>/dev/null && git log --oneline aiforge/$TICKET ^master 2>/dev/null" || true)

PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<EOF
You are Sr Developer REVIEWING Developer's work on $TICKET.

CONTEXT: /Users/manikanta/AIForgeCrew/docs/tickets/$TICKET.md
BREAKDOWN: /Users/manikanta/AIForgeCrew/docs/breakdowns/$TICKET.md
BRANCH: aiforge/$TICKET in MongoDbService.
DEV COMMITS:
$COMMITS

Steps:
1. cat both files (context + breakdown)
2. cd ~/codeRepo/MongoDbService && git checkout aiforge/$TICKET
3. git diff master..aiforge/$TICKET
4. Verify EACH sub-task in breakdown has matching code change
5. mvn -q compile  (JAVA_HOME set, mvn in PATH — just works)
6. mvn -q test -Dtest=<NewTestClass>
7. Test-authenticity check: each @Test must invoke the method under test. No assertTrue(true) stubs. No "tests that test their own setup".
8. Verdict — the dispatcher reads your log for the verdict block, so ALWAYS print exactly:

VERDICT_START
status: READY_FOR_REVIEW  (or NEEDS_DEV_REWORK)
compile: pass|fail (details)
tests: <N> passed / <M> total (failures: ...)
breakdown_match: full|partial: <reason>|no: <reason>
code_quality: <notes>
VERDICT_END

DO NOT modify src/ or tests. You are reviewing, not coding.
Max $TURNS turns.
EOF

PROMPT_REMOTE="/tmp/review-$TICKET.txt"
scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$PROMPT_LOCAL" "$SSH_HOST:$PROMPT_REMOTE"
rm -f "$PROMPT_LOCAL"

HERMES_LOG="/tmp/hermes-review-$TICKET.log"
START=$(date +%s)
echo "launching Review hermes (gemma-4-31b-it, max-turns=$TURNS)..."
remote "~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
END=$(date +%s); WALL=$((END - START))
echo "wall: ${WALL}s"

# Extract verdict block from log (between VERDICT_START and VERDICT_END)
VERDICT=$(remote "awk '/VERDICT_START/,/VERDICT_END/' $HERMES_LOG 2>/dev/null | head -20" || true)
if [[ -z "$VERDICT" ]]; then
  # Fallback: look for NEEDS_DEV_REWORK or READY_FOR_REVIEW markers anywhere
  VERDICT=$(remote "grep -E 'NEEDS_DEV_REWORK|READY_FOR_REVIEW|REVIEW FAILED|REVIEW PASSED' $HERMES_LOG 2>/dev/null | tail -10" || true)
fi
[[ -z "$VERDICT" ]] && VERDICT="(no verdict block found — check $HERMES_LOG)"

echo "VERDICT EXTRACTED:"
echo "$VERDICT"

pc_comment "$ISSUE_UUID" "$(printf 'REVIEW — wall=%ss\n\n%s' "$WALL" "$VERDICT")"
echo "posted review verdict to Paperclip"

# If NEEDS_DEV_REWORK → keep as backlog; if READY → todo
if echo "$VERDICT" | grep -q "READY_FOR_REVIEW"; then
  rpsql "UPDATE issues SET status='todo' WHERE identifier='$TICKET'" >/dev/null
  echo "status → todo (ready for human review)"
else
  echo "status stays backlog (needs dev rework)"
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
