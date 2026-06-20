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
# Strip ownership/ACL lines: the source dump may reference roles that
# don't exist in the fresh compose cluster (only PG_USER is created).
# Everything ends up owned by PG_USER, which is what the app connects as.
_strip() { grep -vE '^(ALTER .* OWNER TO|GRANT |REVOKE |.*OWNER TO )' ; }
if [[ "$DUMP" == *.gz ]]; then
  gunzip -c "$DUMP" | _strip | $DOCKER exec -i "$CONTAINER" psql -v ON_ERROR_STOP=0 -U "$PG_USER" -d "$PG_DB" >/dev/null
else
  _strip < "$DUMP" | $DOCKER exec -i "$CONTAINER" psql -v ON_ERROR_STOP=0 -U "$PG_USER" -d "$PG_DB" >/dev/null
fi

echo "==> ticket count after restore:"
$DOCKER exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc \
  "select count(*) from tickets" 2>/dev/null || echo "(tickets table not found)"
