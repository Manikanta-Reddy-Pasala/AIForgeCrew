#!/usr/bin/env bash
# Back up the existing (bare) Postgres + the running Neo4j container
# BEFORE migrating to docker-compose. Non-destructive — only reads.
#
#   scripts/compose/backup-prod.sh [OUTDIR]
#
# Defaults OUTDIR to ~/aiforge-backups. Produces:
#   pg-<db>-<ts>.sql.gz       — pg_dump of the AIForge database
#   neo4j-data-<ts>.tar.gz    — tar of the live Neo4j /data volume
set -euo pipefail

OUT="${1:-$HOME/aiforge-backups}"
TS="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"

PG_USER="${PG_USER:-aiforge}"
PG_PASSWORD="${PG_PASSWORD:-aiforgepass}"
PG_DB="${PG_DB:-aiforge}"
PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5432}"
NEO4J_CONTAINER="${NEO4J_CONTAINER:-aiforge-neo4j}"
DOCKER="${DOCKER:-sudo docker}"

echo "==> postgres dump → $OUT/pg-${PG_DB}-${TS}.sql.gz"
PGPASSWORD="$PG_PASSWORD" pg_dump \
  -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
  | gzip > "$OUT/pg-${PG_DB}-${TS}.sql.gz"
echo "    $(du -h "$OUT/pg-${PG_DB}-${TS}.sql.gz" | cut -f1)"

if $DOCKER ps --format '{{.Names}}' | grep -qx "$NEO4J_CONTAINER"; then
  echo "==> neo4j /data volume tar → $OUT/neo4j-data-${TS}.tar.gz"
  $DOCKER run --rm --volumes-from "$NEO4J_CONTAINER" \
    -v "$OUT:/backup" busybox \
    tar czf "/backup/neo4j-data-${TS}.tar.gz" /data
  echo "    $(du -h "$OUT/neo4j-data-${TS}.tar.gz" | cut -f1)"
else
  echo "!!  neo4j container '$NEO4J_CONTAINER' not running — skipping graph backup" >&2
fi

echo "==> backups in $OUT:"
ls -lh "$OUT" | tail -5
