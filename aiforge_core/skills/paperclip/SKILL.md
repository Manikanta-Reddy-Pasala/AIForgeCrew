---
name: aiforge-paperclip
description: Direct Paperclip ticket ops — fetch context (title/body/assignee/status), list comments, post comment, transition status, query postgres. Use to read the current ticket state mid-session or to post structured comments. The Paperclip server is on localhost:3100 and postgres at localhost:54329 (user=paperclip/paperclip).
version: 1.0.0
platforms: [macos]
---

# aiforge-paperclip

## Fetch ticket context (title, body, status, assignee, comments)

```bash
TICKET="${TICKET:-ONE-53}"
PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At <<SQL
SELECT json_build_object(
  'id', i.id,
  'identifier', i.identifier,
  'title', i.title,
  'status', i.status,
  'assignee_agent', a.name,
  'body_preview', substring(i.description, 1, 500),
  'comment_count', (SELECT COUNT(*) FROM issue_comments WHERE issue_id=i.id)
) FROM issues i LEFT JOIN agents a ON i.assignee_agent_id=a.id WHERE i.identifier='$TICKET';
SQL
```

## List recent comments

```bash
TICKET="${TICKET:-ONE-53}"
PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \
"SELECT substring(ic.body, 1, 300), ic.created_at FROM issue_comments ic JOIN issues i ON ic.issue_id=i.id WHERE i.identifier='$TICKET' ORDER BY ic.created_at DESC LIMIT 10"
```

## Post a comment (goes to Paperclip UI + triggers webhooks)

```bash
TICKET="${TICKET:-ONE-53}"
BODY="${BODY:-hello from hermes}"
UUID=$(PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c "SELECT id FROM issues WHERE identifier='$TICKET'")
printf '%s' "$BODY" | python3 -c 'import sys,json; print(json.dumps({"body": sys.stdin.read()}))' \
  | curl -sS -X POST "http://localhost:3100/api/issues/$UUID/comments" -H 'Content-Type: application/json' --data @-
```

## Transition status

```bash
TICKET="${TICKET:-ONE-53}"
STATUS="${STATUS:-todo}"   # backlog | todo | in_progress | in_review | blocked | done
PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \
"UPDATE issues SET status='$STATUS' WHERE identifier='$TICKET'"
```

## List agents

```bash
PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -At -c \
"SELECT name, status, adapter_config->>'model' FROM agents WHERE company_id='fd294bd0-2f65-405f-b443-fb41d66226fb' ORDER BY status"
```

## Canonical markers

Use exact strings in comment bodies to signal pipeline transitions:

| Marker | Meaning |
|---|---|
| `READY_FOR_DEV` | Sr Dev breakdown done → Developer may pick up |
| `READY_FOR_REVIEW` | Developer finished → Sr Dev reviews |
| `NEEDS_DEV_REWORK` | Review failed → Developer bounces |
| `NEEDS_HUMAN` | Bounce cap exceeded → human intervention |
| `BOUNCE_ROUND=N` | Emitted on rework attempts for cap counting |

## Danger — don't run these in agent sessions

- `DELETE FROM issues ...`
- `TRUNCATE ...`
- `DROP TABLE ...`

The Paperclip DB is persistent ticket state — destructive SQL needs human approval.
