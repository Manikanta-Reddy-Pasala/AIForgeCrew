#!/usr/bin/env bash
# scripts/install-pgvector-macstudio.sh — install Homebrew + PostgreSQL 16 +
# pgvector on the Mac Studio, create an `aiforge` database with the vector
# extension enabled.
#
# Runs via SSH. Idempotent. Needs passwordless sudo for brew install phase
# OR brew already present.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install-pgvector: macOS only" >&2; exit 1
fi

# --- 1. Homebrew ---
if ! command -v brew >/dev/null; then
  echo ">>> installing Homebrew"
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# Apple Silicon brew lands at /opt/homebrew — add to PATH for this run.
if [[ -x /opt/homebrew/bin/brew ]]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi
brew --version

# --- 2. Postgres 16 + pgvector ---
if ! brew list postgresql@16 >/dev/null 2>&1; then
  echo ">>> brew install postgresql@16"
  brew install postgresql@16
fi
if ! brew list pgvector >/dev/null 2>&1; then
  echo ">>> brew install pgvector"
  brew install pgvector
fi

PGBIN="/opt/homebrew/opt/postgresql@16/bin"
export PATH="$PGBIN:$PATH"

# --- 3. Start service ---
# brew services uses launchctl under the hood (plist at ~/Library/LaunchAgents).
brew services restart postgresql@16
sleep 5

# --- 4. Create aiforge db + enable extension ---
# brew postgres runs as current user, no password required from localhost.
createdb aiforge 2>/dev/null || echo "  [skip] db aiforge exists"
psql -d aiforge -c "CREATE EXTENSION IF NOT EXISTS vector" 2>&1 | tail -2

psql -d aiforge -c "
CREATE TABLE IF NOT EXISTS memories (
    id          BIGSERIAL PRIMARY KEY,
    wing        TEXT NOT NULL,
    room        TEXT,
    source      TEXT,
    title       TEXT,
    text        TEXT NOT NULL,
    embedding   vector(768),
    metadata    JSONB DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memories_wing    ON memories (wing);
CREATE INDEX IF NOT EXISTS idx_memories_source  ON memories (source);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories (created_at DESC);
-- HNSW index for cosine similarity on nomic-embed-text (768-dim).
-- Built after data is bulk-loaded for performance; safe to create empty.
CREATE INDEX IF NOT EXISTS idx_memories_embedding
    ON memories USING hnsw (embedding vector_cosine_ops);
" 2>&1 | tail -5

echo
echo "=== verify ==="
psql -d aiforge -c "SELECT extname, extversion FROM pg_extension WHERE extname='vector'" 2>&1 | head
psql -d aiforge -c "SELECT count(*) AS memories_table_count FROM memories" 2>&1 | tail -3
echo
echo "Postgres (brew) running on :5432. DB=aiforge. User=$(whoami)."
echo "Next: bash scripts/pgmem-import.sh to load codeRepo + Claude memory."
