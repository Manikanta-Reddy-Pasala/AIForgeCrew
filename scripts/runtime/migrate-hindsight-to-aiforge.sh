#!/usr/bin/env bash
# One-shot migration — copies hindsight `memory_units` (bank=aiforge) into
# aiforge.memories with tier=t2, wing=rules/canon. Idempotent on identifier
# collisions (uses content sha1 as metadata.hs_src_id).
#
# Runs ON Mac Studio (pg0 lives there). Dry-run first.
#
#   DRY_RUN=1 bash scripts/runtime/migrate-hindsight-to-aiforge.sh
#   bash scripts/runtime/migrate-hindsight-to-aiforge.sh
set -euo pipefail

HINDSIGHT_DSN="${HINDSIGHT_DSN:-postgresql://hindsight:hindsight@127.0.0.1:5433/hindsight}"
AIFORGE_DSN="${AIFORGE_DSN:-postgresql://manikanta@127.0.0.1:5432/aiforge}"
PSQL="/Users/manikanta/.pg0/installation/18.1.0/bin/psql"

echo ">>> hindsight bank count"
PGPASSWORD=hindsight "$PSQL" "$HINDSIGHT_DSN" -At \
  -c "SELECT COUNT(*) FROM memory_units WHERE bank_id='aiforge'"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo ">>> DRY_RUN: preview first 3 rows"
  PGPASSWORD=hindsight "$PSQL" "$HINDSIGHT_DSN" \
    -c "SELECT LEFT(content, 120) AS preview, fact_type, created_at
        FROM memory_units WHERE bank_id='aiforge' ORDER BY created_at LIMIT 3"
  exit 0
fi

echo ">>> migrating hindsight → aiforge (tier=t2, wing=rules/canon)"
# hindsight schema: text = main payload, context = surrounding sentence
PGPASSWORD=hindsight "$PSQL" "$HINDSIGHT_DSN" \
  -c "\copy (SELECT text, COALESCE(context,''), fact_type, bank_id, created_at
             FROM memory_units WHERE bank_id='aiforge')
      TO '/tmp/hs-export.tsv'"

"$PSQL" "$AIFORGE_DSN" <<'SQL'
\set ON_ERROR_STOP on
BEGIN;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TEMP TABLE hs_import (
  text_body text, ctx text, fact_type text, bank_id text, created_at timestamptz
);
\copy hs_import FROM '/tmp/hs-export.tsv'
INSERT INTO memories (tier, wing, kind, text, metadata, created_at)
SELECT
  't2', 'rules/canon', 'fact',
  text_body,
  jsonb_build_object(
    'source','hindsight',
    'fact_type', fact_type,
    'bank', bank_id,
    'context', ctx,
    'hs_src_sha1', encode(digest(text_body,'sha1'),'hex')
  ),
  created_at
FROM hs_import
WHERE NOT EXISTS (
  SELECT 1 FROM memories m
  WHERE m.tier='t2' AND m.wing='rules/canon'
    AND m.metadata->>'hs_src_sha1' = encode(digest(hs_import.text_body,'sha1'),'hex')
);
SELECT COUNT(*) AS total_rules_canon FROM memories WHERE tier='t2' AND wing='rules/canon';
COMMIT;
SQL

rm -f /tmp/hs-export.tsv
echo ">>> migrated. embedding backfill runs separately (embed-backfill.py)."
