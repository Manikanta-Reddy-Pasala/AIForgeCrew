#!/usr/bin/env bash
# Load a pg_dump (.sql or .sql.gz) into the compose Postgres container.
# Run AFTER `docker compose up -d postgres`.
#
#   scripts/compose/restore-pg.sh <dump.sql[.gz]>
set -euo pipefail

DUMP="${1:?usage: restore-pg.sh <dump.sql[.gz]>}"
PG_USER="${PG_USER:-aiforge}"
PG_DB="${PG_DB:-aiforge}"
CONTAINER="${PG_CONTAINER:-aiforge-postgres}"
DOCKER="${DOCKER:-sudo docker}"

echo "==> waiting for $CONTAINER to be ready"
for _ in $(seq 1 30); do
  if $DOCKER exec "$CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "==> restoring $DUMP into $CONTAINER:$PG_DB"
if [[ "$DUMP" == *.gz ]]; then
  gunzip -c "$DUMP" | $DOCKER exec -i "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB"
else
  $DOCKER exec -i "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" < "$DUMP"
fi

echo "==> ticket count after restore:"
$DOCKER exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc \
  "select count(*) from tickets" 2>/dev/null || echo "(tickets table not found)"
