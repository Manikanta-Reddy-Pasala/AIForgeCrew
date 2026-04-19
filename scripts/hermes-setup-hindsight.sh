#!/usr/bin/env bash
# scripts/hermes-setup-hindsight.sh — enable Hindsight as the Hermes memory
# provider (replaces MemPalace + custom pgmem).
#
# Hindsight: local Postgres + knowledge graph + 4-strategy retrieval
# (semantic / BM25 / graph traversal / temporal) + cross-encoder rerank.
# Shipped by NousResearch as a first-party memory provider.
#
# Runs `hermes memory setup hindsight` which:
#   1. ensures Postgres 16 is available (brew install if missing)
#   2. creates `hindsight` database + schema
#   3. writes ~/.hermes/memory.yaml with provider: hindsight
#   4. writes per-profile overrides under ~/.hermes/profiles/*.yaml
#
# Idempotent.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "hermes-setup-hindsight: macOS only" >&2; exit 1; }

HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
HERMES_DIR="${HERMES_DIR:-$HOME/.hermes}"

[[ -x "$HERMES_BIN" ]] || { echo "hermes CLI missing — run scripts/install-hermes-agent.sh" >&2; exit 1; }

export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"

# ---- 1. Ensure Postgres is up (Hindsight stores its graph there) ----
if ! command -v psql >/dev/null; then
  echo ">>> installing postgresql@16 (Hindsight dependency)"
  command -v brew >/dev/null || { echo "brew missing — run scripts/install-pgvector-macstudio.sh" >&2; exit 1; }
  brew install postgresql@16
fi

brew services list 2>/dev/null | grep -q "^postgresql@16.*started" || \
  brew services start postgresql@16
sleep 3

# ---- 2. Install Hindsight provider (git clone if not already done) ----
SKILL_DIR="$HERMES_DIR/skills/hindsight"
if [[ ! -d "$SKILL_DIR" ]]; then
  echo ">>> cloning Hindsight provider"
  mkdir -p "$HERMES_DIR/skills"
  tmp="/tmp/hermes-hindsight-$$"
  git clone --depth 1 --quiet https://github.com/NousResearch/hermes-agent.git "$tmp"
  mv "$tmp/plugins/memory/hindsight" "$SKILL_DIR"
  rm -rf "$tmp"
fi

# ---- 3. Configure Hermes to use Hindsight ----
# Memory config lives in ~/.hermes/memory.yaml. Hermes reads this on agent start.
cat > "$HERMES_DIR/memory.yaml" <<'EOF'
# ~/.hermes/memory.yaml — written by scripts/hermes-setup-hindsight.sh
provider: hindsight
config:
  # Local Postgres (brew postgresql@16). aiforge db is reused — Hindsight
  # gets its own schema inside it so we don't run two postgres instances.
  dsn: "postgresql://${USER}@127.0.0.1:5432/aiforge"
  schema: hindsight

  # 4-strategy retrieval knobs.
  strategies:
    semantic:   { enabled: true, top_k: 20 }
    bm25:       { enabled: true, top_k: 20 }
    graph:      { enabled: true, hops: 2 }
    temporal:   { enabled: true, window_days: 90 }

  # Cross-encoder rerank for final top-k.
  rerank:
    enabled: true
    top_k: 8

  # 15-call reflection cycle triggers (Hermes default). Skills auto-written
  # to ~/.hermes/skills/ when patterns succeed.
  reflection:
    enabled: true
    every_calls: 15
EOF

# Ensure the `hindsight` schema exists in the aiforge db.
psql -d aiforge -c "CREATE SCHEMA IF NOT EXISTS hindsight" >/dev/null

# Trigger Hermes to run any schema migrations Hindsight ships with.
"$HERMES_BIN" memory migrate 2>/dev/null || echo "  (memory migrate not available; Hindsight creates tables lazily)"

# ---- 4. Per-role memory isolation ----
# Each role gets its own Hermes profile dir so their CALLS/skills don't clobber
# each other, but they SHARE the Hindsight knowledge graph (project memory).
# Role isolation is enforced at the aiforge_core skill layer, not the DB.
for role in em tester sr-developer sr-architect; do
  mkdir -p "$HERMES_DIR/profiles/$role"
  # Profile-local memory.yaml inherits global. Override only if needed.
  cat > "$HERMES_DIR/profiles/${role}.memory.yaml" <<EOF
# ${role} memory — shared Hindsight knowledge graph, per-role session archive.
inherit: ~/.hermes/memory.yaml
config:
  session_namespace: "${role}"
EOF
done

echo
echo "Hindsight configured:"
echo "  provider      hindsight"
echo "  backend       postgresql://${USER}@127.0.0.1:5432/aiforge (schema: hindsight)"
echo "  strategies    semantic + bm25 + graph + temporal + rerank"
echo "  reflection    every 15 calls → writes skills to ~/.hermes/skills/"
echo "  roles         em · tester · sr-developer · sr-architect (shared KG)"
echo
echo "Next:"
echo "  scripts/hermes-seed-memory.sh   # import existing Claude memory"
