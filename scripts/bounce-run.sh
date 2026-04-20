#!/usr/bin/env bash
# Bounce Developer back on a ticket after review posted NEEDS_DEV_REWORK.
# Extracts review verdict, prepends to Developer prompt, re-runs on same branch.
set -uo pipefail

TICKET="${1:?usage: $0 <TICKET-ID>}"
SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
MODEL="qwen3-coder-next"
TURNS=200
DEV_ID="e0502e94-0608-4fb9-9afa-b70d8dbf014a"
CID="fd294bd0-2f65-405f-b443-fb41d66226fb"

remote() { ssh -o IdentitiesOnly=yes -o ServerAliveInterval=60 -o ServerAliveCountMax=30 -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }
rpsql() { remote "PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"$1\""; }
pc_comment() {
  local uuid="$1" body="$2"
  local json=$(printf '%s' "$body" | python3 -c 'import sys,json; print(json.dumps({"body": sys.stdin.read()}))')
  echo "$json" | remote "curl -sS -X POST 'http://localhost:3100/api/issues/$uuid/comments' -H 'Content-Type: application/json' --data @-" >/dev/null
}

echo "=== Bounce (Developer rework): $TICKET ==="

ISSUE_UUID=$(rpsql "SELECT id FROM issues WHERE identifier='$TICKET'")
[[ -z "$ISSUE_UUID" ]] && { echo "ticket $TICKET not found"; exit 1; }

# Fetch the most recent REVIEW verdict comment (contains NEEDS_DEV_REWORK or VERDICT_START)
REVIEW=$(rpsql "SELECT body FROM issue_comments WHERE issue_id='$ISSUE_UUID' AND (body LIKE '%VERDICT_START%' OR body LIKE '%NEEDS_DEV_REWORK%') ORDER BY created_at DESC LIMIT 1")
if [[ -z "$REVIEW" ]]; then
  echo "no review verdict found for $TICKET. run scripts/review-run.sh first."
  exit 1
fi
echo "  review verdict (head):"
echo "$REVIEW" | head -10 | sed 's/^/    /'

# Bounce counter: count prior bounce comments to cap iterations
BOUNCES=$(rpsql "SELECT COUNT(*) FROM issue_comments WHERE issue_id='$ISSUE_UUID' AND body LIKE '%BOUNCE_ROUND%'")
BOUNCE_NEXT=$((BOUNCES + 1))
MAX_BOUNCES=${MAX_BOUNCES:-3}
if (( BOUNCE_NEXT > MAX_BOUNCES )); then
  MSG="BOUNCE_ROUND $BOUNCE_NEXT — exceeded max ($MAX_BOUNCES). NEEDS_HUMAN."
  pc_comment "$ISSUE_UUID" "$MSG"
  echo "$MSG"
  exit 1
fi
echo "  bounce round: $BOUNCE_NEXT / $MAX_BOUNCES"

rpsql "UPDATE issues SET assignee_agent_id='$DEV_ID', status='backlog' WHERE identifier='$TICKET'" >/dev/null

PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<EOF
You are Developer. Ticket $TICKET. REWORK round $BOUNCE_NEXT.

Review from Sr Dev flagged problems with your prior submission. FIX the specific issues. Do NOT redo what's already correct.

REVIEW VERDICT:
$REVIEW

MANDATORY FIRST STEPS:
1. cat /Users/manikanta/AIForgeCrew/docs/tickets/$TICKET.md
2. cat /Users/manikanta/AIForgeCrew/docs/breakdowns/$TICKET.md
3. cd ~/codeRepo/MongoDbService && git fetch origin && git checkout aiforge/$TICKET
4. git diff master..aiforge/$TICKET   (see your prior commits)

Now address EACH failure point from the review verdict above:
- If review says "fake tests" → rewrite tests to actually invoke method under test with Mockito ArgumentCaptor. NO assertTrue(true) stubs. NO string-analysis. Real mock, real invocation, real assertions.
- If review says "implementation wrong" (e.g. "two updateOne calls instead of one") → fix the implementation. The breakdown's target line numbers + acceptance criteria are still the spec. Re-read them carefully.
- If review says "breakdown_match: no/partial" → apply the exact change described in the breakdown.

Run mvn -q compile. Then mvn -q test -Dtest=<TestClass>. Both MUST pass.

If tests pass but the printed output contradicts the acceptance criterion (e.g. test prints "updateOne=2" when spec requires 1) — that's still wrong. The test's assertions need to also enforce the spec.

Commit with message: fix(<area>): $TICKET.<N> rework round $BOUNCE_NEXT — <what you fixed>.
Push. Do NOT open a new PR — the existing one will update.

Max $TURNS turns.
EOF

PROMPT_REMOTE="/tmp/bounce-$TICKET-round$BOUNCE_NEXT.txt"
scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$PROMPT_LOCAL" "$SSH_HOST:$PROMPT_REMOTE"
rm -f "$PROMPT_LOCAL"

HERMES_LOG="/tmp/hermes-bounce-$TICKET-round$BOUNCE_NEXT.log"
START=$(date +%s)
echo "launching Developer rework hermes (qwen3-coder-next)..."
remote "~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
END=$(date +%s); WALL=$((END - START))
echo "wall: ${WALL}s"

# Summarize post-run
SUMMARY="BOUNCE_ROUND $BOUNCE_NEXT — wall=${WALL}s"
COMMITS=$(remote "cd ~/codeRepo/MongoDbService && git log --oneline aiforge/$TICKET ^master 2>/dev/null" || true)
SUMMARY+=$'\n\nCommits on aiforge/'"$TICKET"$':\n'"$COMMITS"

# Run mvn test on the new test class (pick up any new test files too)
if remote "test -f ~/codeRepo/MongoDbService/pom.xml"; then
  BUILD=$(remote "cd ~/codeRepo/MongoDbService && git checkout aiforge/$TICKET >/dev/null 2>&1 && mvn -q -DskipTests compile 2>&1 | tail -5" || true)
  if echo "$BUILD" | grep -qE "BUILD FAILURE|ERROR"; then
    SUMMARY+=$'\n\n'"mvn compile: FAIL"$'\n'"$BUILD"
  else
    SUMMARY+=$'\n\n'"mvn compile: OK"
    # Run whatever atomic-ish test exists
    TESTS=$(remote "cd ~/codeRepo/MongoDbService && find src/test -name 'ProductServiceImpl*Test*.java' 2>/dev/null | xargs -I{} basename {} .java")
    for t in $TESTS; do
      TOUT=$(remote "cd ~/codeRepo/MongoDbService && mvn -q test -Dtest=$t 2>&1 | tail -5")
      RESULT=$(echo "$TOUT" | grep -oE "Tests run: [^,]+|BUILD FAILURE" | head -1)
      SUMMARY+=$'\n'"mvn test -Dtest=$t: $RESULT"
    done
  fi
fi

# Push updated branch
remote "cd ~/codeRepo/MongoDbService && git push origin aiforge/$TICKET 2>&1 | tail -2" >/dev/null
PR=$(remote "cd ~/codeRepo/MongoDbService && gh pr list --head aiforge/$TICKET --json url --jq '.[0].url' 2>/dev/null" || echo "")
SUMMARY+=$'\n'"PR (updated): $PR"

SUMMARY+=$'\n\n'"Ready for re-review. Dispatch scripts/review-run.sh $TICKET."

pc_comment "$ISSUE_UUID" "$SUMMARY"
echo "posted bounce summary"
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
