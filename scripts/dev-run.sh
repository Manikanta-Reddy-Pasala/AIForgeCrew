#!/usr/bin/env bash
# Dispatch Developer. Disable Paperclip retry during run.
# Enforce mvn compile + mvn test + open PR at dispatcher level (not agent).
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

echo "=== Developer: $TICKET ==="

# Sync latest ticket + breakdown
for f in "docs/tickets/$TICKET.md" "docs/breakdowns/$TICKET.md"; do
  [[ -f "$f" ]] && scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$f" "$SSH_HOST:~/AIForgeCrew/$f"
done

ISSUE_UUID=$(rpsql "SELECT id FROM issues WHERE identifier='$TICKET'")
rpsql "UPDATE issues SET assignee_agent_id='$DEV_ID', status='backlog' WHERE identifier='$TICKET'" >/dev/null
echo "  ticket set to backlog (paperclip won't auto-retry)"

PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<EOF
You are Developer. Ticket $TICKET.

MANDATORY FIRST STEPS:
1. cat /Users/manikanta/AIForgeCrew/docs/tickets/$TICKET.md        # Architect
2. cat /Users/manikanta/AIForgeCrew/docs/breakdowns/$TICKET.md      # Sr Dev
3. For each involved repo named in context:
     cd ~/codeRepo/<repo> && git fetch origin && git checkout aiforge/$TICKET

Per sub-task in the breakdown:
4. Read target file:line
5. Apply change (patch or write_file) — MINIMUM viable change only
6. Write unit test matching Sr Dev's test case in plain English. Test MUST invoke the method under test — no assertTrue(true) stubs, no "tests that test their own setup".
7. For Java (MongoDbService/etc): mvn -q compile  — if this fails, FIX imports/deps BEFORE committing.
8. For Java: mvn -q test -Dtest=<YourNewTestClass>  — if fails, FIX test or impl BEFORE committing.
9. For Python (PosPythonBackend): /usr/bin/python3 -m pytest tests/path/test_xxx.py — if fails, fix.
10. Only after compile+test pass: git add -A && git commit -m "fix(<area>): $TICKET.<N> <desc>"

After all sub-tasks done:
11. git push -u origin aiforge/$TICKET
12. gh pr create --base master --title "$TICKET <title>" --body "<sub-task list>" --assignee @me

DO NOT commit code that fails to compile.
DO NOT commit tests that don't actually run the method under test.
DO NOT skip mvn compile/test — JAVA_HOME is set, mvn is in PATH.

Max $TURNS turns.
EOF

PROMPT_REMOTE="/tmp/dev-$TICKET.txt"
scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$PROMPT_LOCAL" "$SSH_HOST:$PROMPT_REMOTE"
rm -f "$PROMPT_LOCAL"

HERMES_LOG="/tmp/hermes-dev-$TICKET.log"
START=$(date +%s)
echo "launching Developer hermes (qwen3-coder-next, max-turns=$TURNS)..."
remote "~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
END=$(date +%s); WALL=$((END - START))
echo "wall: ${WALL}s"

# Post-run: for each involved repo, run mvn compile + mvn test + push + PR
INVOLVED=()
for repo in PosPythonBackend MongoDbService TallyConnector PosServerBackend PosDataSyncService; do
  C=$(remote "cd ~/codeRepo/$repo 2>/dev/null && git log --oneline aiforge/$TICKET ^master 2>/dev/null | wc -l | tr -d ' '" || echo 0)
  [[ "$C" != "0" ]] && INVOLVED+=("$repo")
done

SUMMARY="DEVELOPER RUN — wall=${WALL}s"
if [[ ${#INVOLVED[@]} -eq 0 ]]; then
  SUMMARY+=$'\n\n'"No commits produced on aiforge/$TICKET in any repo. Likely Developer exited without committing — check $HERMES_LOG."
else
  SUMMARY+=$'\n\n'"Involved repos: ${INVOLVED[*]}"
fi

for repo in "${INVOLVED[@]+${INVOLVED[@]}}"; do
  SUMMARY+=$'\n\n'"### $repo"
  if remote "test -f ~/codeRepo/$repo/pom.xml"; then
    # Java repo: mvn compile + mvn test
    BUILD=$(remote "cd ~/codeRepo/$repo && git checkout aiforge/$TICKET >/dev/null 2>&1 && mvn -q -DskipTests compile 2>&1 | tail -8" || true)
    if echo "$BUILD" | grep -qE "BUILD FAILURE|ERROR|Fatal"; then
      SUMMARY+=$'\n'"- mvn compile: FAIL — $(echo "$BUILD" | grep -E 'ERROR|Fatal' | head -1)"
      SUMMARY+=$'\n'"- NOT pushing, NOT opening PR"
      continue
    fi
    SUMMARY+=$'\n'"- mvn compile: OK"
    TESTS=$(remote "cd ~/codeRepo/$repo && git diff --name-only --diff-filter=A master..aiforge/$TICKET 2>/dev/null | grep -E 'src/test/.*\.java$' | xargs -I{} basename {} .java" || true)
    for t in $TESTS; do
      TOUT=$(remote "cd ~/codeRepo/$repo && mvn -q test -Dtest=$t 2>&1 | tail -5" || true)
      RESULT=$(echo "$TOUT" | grep -oE "Tests run: [^,]+|BUILD FAILURE" | head -1)
      SUMMARY+=$'\n'"- mvn test -Dtest=$t: $RESULT"
    done
  elif remote "test -f ~/codeRepo/$repo/pyproject.toml -o -f ~/codeRepo/$repo/requirements.txt"; then
    # Python repo: uv run pytest
    TESTS=$(remote "cd ~/codeRepo/$repo && git diff --name-only --diff-filter=A master..aiforge/$TICKET 2>/dev/null | grep -E 'tests?/.*test.*\.py$'" || true)
    if [[ -n "$TESTS" ]]; then
      for tf in $TESTS; do
        TOUT=$(remote "cd ~/codeRepo/$repo && ~/.local/bin/uv run pytest '$tf' -v --tb=line 2>&1 | tail -5" || true)
        RESULT=$(echo "$TOUT" | grep -oE "[0-9]+ passed|[0-9]+ failed|error" | head -2 | tr '\n' ' ')
        SUMMARY+=$'\n'"- uv run pytest $tf: $RESULT"
      done
    else
      SUMMARY+=$'\n'"- python repo: no new test files"
    fi
  fi
  # Push + PR
  remote "cd ~/codeRepo/$repo && git push -u origin aiforge/$TICKET 2>&1 | tail -2" >/dev/null
  PR=$(remote "cd ~/codeRepo/$repo && gh pr list --head aiforge/$TICKET --json url --jq '.[0].url' 2>/dev/null" || echo "")
  if [[ -z "$PR" ]]; then
    PR=$(remote "cd ~/codeRepo/$repo && gh pr create --base master --title '$TICKET auto-PR' --body 'Auto-opened. See docs/breakdowns/$TICKET.md for sub-tasks.' 2>&1 | tail -1" || echo "(pr create failed)")
  fi
  SUMMARY+=$'\n'"- PR: $PR"
done

echo "$SUMMARY"
pc_comment "$ISSUE_UUID" "$SUMMARY"$'\n\nREADY_FOR_REVIEW'
echo "posted READY_FOR_REVIEW comment"
rpsql "UPDATE issues SET status='todo' WHERE identifier='$TICKET'" >/dev/null

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
