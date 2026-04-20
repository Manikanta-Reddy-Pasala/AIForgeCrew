#!/usr/bin/env bash
# Bootstrap the `aiforge` Postgres database for v4.1 memory.
# Usage:
#   bash scripts/install-pg-aiforge.sh [--dry-run]
#   SSH_HOST=manikanta@192.168.70.185 bash scripts/install-pg-aiforge.sh
set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; fi

SQL=$(cat <<'EOF'
CREATE DATABASE aiforge;
\c aiforge
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- CREATE TABLE memories
CREATE TABLE IF NOT EXISTS memories (
    id          BIGSERIAL PRIMARY KEY,
    tier        TEXT NOT NULL CHECK (tier IN ('t1','t2','t3','t4')),
    wing        TEXT NOT NULL,
    parent_id   TEXT,
    kind        TEXT NOT NULL,
    source      TEXT,
    title       TEXT,
    text        TEXT NOT NULL,
    embedding   vector(1024),
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_memories_tier_wing  ON memories(tier, wing);
CREATE INDEX IF NOT EXISTS idx_memories_parent     ON memories(parent_id);
CREATE INDEX IF NOT EXISTS idx_memories_expires    ON memories(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_embedding  ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_memories_text_trgm  ON memories USING gin (text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_memories_title_trgm ON memories USING gin (title gin_trgm_ops);
-- CREATE TABLE memory_proposals
CREATE TABLE IF NOT EXISTS memory_proposals (
    id           BIGSERIAL PRIMARY KEY,
    tier         TEXT NOT NULL CHECK (tier IN ('t2','t3')),
    wing         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    title        TEXT,
    text         TEXT NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_trace TEXT NOT NULL,
    proposed_by  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ,
    decided_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON memory_proposals(status);
EOF
)

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "$SQL"
  exit 0
fi

PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-$USER}"

psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d postgres -v ON_ERROR_STOP=0 <<< "$SQL"
echo "aiforge DB ready at $PG_HOST:$PG_PORT"
