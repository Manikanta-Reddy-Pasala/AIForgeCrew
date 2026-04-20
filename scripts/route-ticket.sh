#!/usr/bin/env bash
# scripts/route-ticket.sh — assign a Paperclip issue to the right engineer
# based on reasoning vs code classification, and apply the matching label.
#
# Usage:  bash scripts/route-ticket.sh <TICKET_ID> <reasoning|code>
#   e.g.  bash scripts/route-ticket.sh ONE-50 reasoning
set -euo pipefail

SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
CID="${CID:-fd294bd0-2f65-405f-b443-fb41d66226fb}"

# Agent IDs
SRDEV_REASONING="28b8c064-bfcf-44e1-9e91-e37c39e0097c"   # gemma-4-31b-it (dense)
SRDEV_CODER="e0502e94-0608-4fb9-9afa-b70d8dbf014a"        # qwen3-coder-next (MoE 80B/3B)

# Label IDs
LABEL_REASONING="db58c603-5c1d-47f8-ae3b-59bb13486216"
LABEL_CODE="3d471283-6dd3-408a-9ae4-61465833d33b"

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <TICKET_ID> <reasoning|code>" >&2
  exit 1
fi

TICKET="$1"
KIND="$2"

case "$KIND" in
  reasoning) ASSIGNEE="$SRDEV_REASONING"; LABEL="$LABEL_REASONING"; MODEL="gemma-4-31b-it" ;;
  code)      ASSIGNEE="$SRDEV_CODER";    LABEL="$LABEL_CODE";       MODEL="qwen3-coder-next" ;;
  *) echo "kind must be 'reasoning' or 'code'" >&2; exit 1 ;;
esac

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

# Get issue UUID from identifier
ISSUE_ID=$(remote "PGPASSWORD=paperclip \$HOME/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \"SELECT id FROM issues WHERE identifier='$TICKET'\"")
if [[ -z "$ISSUE_ID" ]]; then
  echo "no issue found for $TICKET" >&2
  exit 1
fi

echo "routing $TICKET → $KIND ($MODEL)"

# Assign
remote "curl -s -X PATCH 'http://localhost:3100/api/issues/$ISSUE_ID' -H 'Content-Type: application/json' -d '{\"assigneeAgentId\":\"$ASSIGNEE\"}' > /dev/null"

# Apply label (add to labelIds list)
remote "curl -s -X POST 'http://localhost:3100/api/issues/$ISSUE_ID/labels' -H 'Content-Type: application/json' -d '{\"labelId\":\"$LABEL\"}' > /dev/null" || true

echo "  assignee: $ASSIGNEE"
echo "  label: $KIND"
echo "  model: $MODEL"
echo "done"
