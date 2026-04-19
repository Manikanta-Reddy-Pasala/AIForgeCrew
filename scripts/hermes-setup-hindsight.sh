#!/usr/bin/env bash
# scripts/hermes-setup-hindsight.sh — enable Hindsight as the Hermes memory
# provider in LOCAL_EMBEDDED mode (replaces MemPalace + custom pgmem).
#
# Hindsight local_embedded:
#   - Spins up a local Hindsight daemon with built-in PostgreSQL
#   - Uses LM Studio (OpenAI-compatible) as the extraction/synthesis LLM
#   - Daemon auto-starts on first use, idles out after 5 minutes
#   - 4-strategy retrieval (semantic / BM25 / graph / temporal) + rerank
#   - 15-call reflection cycle auto-writes skills to ~/.hermes/skills/
#
# Non-interactive — writes ~/.hermes/hindsight/config.json directly
# instead of driving the curses wizard.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "hermes-setup-hindsight: macOS only" >&2; exit 1; }

HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
HERMES_DIR="${HERMES_DIR:-$HOME/.hermes}"

# Hindsight does structured fact extraction on retain() and requires the LLM
# to accept `response_format={"type": "json_object"}`. LM Studio (as of
# 2026-04) rejects this with "'response_format.type' must be 'json_schema'
# or 'text'" — so we route Hindsight to NVIDIA NIM by default. Local MLX
# models via LM Studio remain the primary runtime for agents, Hindsight
# just needs a different endpoint for its own internal LLM calls.
LLM_ENDPOINT="${LLM_ENDPOINT:-https://integrate.api.nvidia.com/v1}"
LLM_MODEL="${LLM_MODEL:-meta/llama-3.3-70b-instruct}"
BANK_ID="${BANK_ID:-aiforge}"
RECALL_BUDGET="${RECALL_BUDGET:-mid}"

# Pull NVIDIA key from ~/.hermes/.env (written by scripts/hermes-configure.sh).
if [[ -z "${LLM_API_KEY:-}" && -f "$HERMES_DIR/.env" ]]; then
  LLM_API_KEY=$(grep "^NVIDIA_API_KEY=" "$HERMES_DIR/.env" | cut -d= -f2-)
fi
LLM_API_KEY="${LLM_API_KEY:-}"

[[ -x "$HERMES_BIN" ]] || { echo "hermes CLI missing — run scripts/install-hermes-agent.sh" >&2; exit 1; }

# ---- 1. Ensure Hindsight plugin is present ----
SKILL_DIR="$HERMES_DIR/skills/hindsight"
if [[ ! -f "$SKILL_DIR/__init__.py" ]]; then
  echo ">>> cloning Hindsight plugin"
  mkdir -p "$HERMES_DIR/skills"
  tmp="/tmp/hermes-hindsight-$$"
  git clone --depth 1 --quiet https://github.com/NousResearch/hermes-agent.git "$tmp"
  rm -rf "$SKILL_DIR"
  mv "$tmp/plugins/memory/hindsight" "$SKILL_DIR"
  rm -rf "$tmp"
fi

# ---- 2. Install Hindsight runtime deps into Hermes venv ----
# For local_embedded mode we need hindsight-all (pulls in hindsight, hindsight-client,
# hindsight-embed, torch/transformers for local embedding + reranking).
# Hermes venv has no pip — use uv (installed by scripts/install-aiforge.sh).
HERMES_VENV_PY="$HERMES_DIR/hermes-agent/venv/bin/python"
UV="${UV:-$HOME/.local/bin/uv}"
if [[ -x "$HERMES_VENV_PY" && -x "$UV" ]]; then
  echo ">>> installing hindsight-all into Hermes venv (may take ~2 min — pulls torch/transformers)"
  "$UV" pip install --python "$HERMES_VENV_PY" --quiet --upgrade "hindsight-all" 2>&1 | tail -3
fi

# ---- 3. Write Hindsight config.json (local_embedded + LM Studio) ----
HINDSIGHT_CFG="$HERMES_DIR/hindsight"
mkdir -p "$HINDSIGHT_CFG"
if [[ -z "$LLM_API_KEY" ]]; then
  echo "WARN: no NVIDIA_API_KEY found — set it in $HERMES_DIR/.env or export LLM_API_KEY" >&2
  LLM_API_KEY="missing"
fi

cat > "$HINDSIGHT_CFG/config.json" <<EOF
{
  "mode": "local_embedded",
  "llm_provider": "openai_compatible",
  "llm_base_url": "$LLM_ENDPOINT",
  "llm_model": "$LLM_MODEL",
  "llm_api_key": "$LLM_API_KEY",
  "bank_id": "$BANK_ID",
  "recall_budget": "$RECALL_BUDGET"
}
EOF

# Also write the profile .env that the daemon reads on start. Daemon
# requires HINDSIGHT_API_LLM_* env vars (it does NOT read config.json
# directly — only the Hermes plugin's _start_daemon hook reads config.json
# and writes the .env). For standalone seed runs we need to write it too.
mkdir -p "$HOME/.hindsight/profiles"
cat > "$HOME/.hindsight/profiles/hermes.env" <<EOF
HINDSIGHT_API_LLM_PROVIDER=openai
HINDSIGHT_API_LLM_API_KEY=$LLM_API_KEY
HINDSIGHT_API_LLM_MODEL=$LLM_MODEL
HINDSIGHT_API_LOG_LEVEL=info
HINDSIGHT_API_LLM_BASE_URL=$LLM_ENDPOINT
EOF
chmod 600 "$HOME/.hindsight/profiles/hermes.env"
echo ">>> wrote $HINDSIGHT_CFG/config.json (local_embedded, LLM=$LLM_MODEL via $LLM_ENDPOINT)"

# ---- 4. Flip the Hermes memory provider flag ----
# `hermes config set memory.provider hindsight` — persisted to ~/.hermes/config.yaml
"$HERMES_BIN" config set memory.provider hindsight >/dev/null 2>&1 || true

# Hermes config.yaml needs a `memory:` block written by hand if `config set`
# refuses dotted paths for a missing key.
if ! grep -q "^memory:" "$HERMES_DIR/config.yaml" 2>/dev/null; then
  cat >> "$HERMES_DIR/config.yaml" <<'EOF'

memory:
  provider: hindsight
EOF
fi

# ---- 5. Per-role profile overrides (shared bank, per-role session namespace) ----
# All four roles (em / tester / sr-developer / sr-architect) share bank_id=aiforge
# so the knowledge graph is project-wide; they differ only in session_namespace
# which scopes recent session history.
mkdir -p "$HERMES_DIR/profiles"
for role in em tester sr-developer sr-architect; do
  cat > "$HERMES_DIR/profiles/${role}.memory.yaml" <<EOF
# ${role} — shared Hindsight bank 'aiforge'; per-role session namespace.
memory:
  provider: hindsight
  session_namespace: "${role}"
EOF
done

# ---- 6. Verify ----
echo
echo "=== hermes memory status ==="
"$HERMES_BIN" memory status 2>&1 | head -20 || true

echo
echo "Hindsight configured:"
echo "  mode          local_embedded  (Hermes-managed Postgres daemon)"
echo "  LLM           $LLM_MODEL via $LLM_ENDPOINT"
echo "  bank_id       $BANK_ID  (shared across all 4 roles)"
echo "  recall_budget $RECALL_BUDGET"
echo "  daemon logs   ~/.hermes/logs/hindsight-embed.log"
echo
echo "Next:"
echo "  scripts/hermes-seed-memory.sh   # import existing Claude memory"
