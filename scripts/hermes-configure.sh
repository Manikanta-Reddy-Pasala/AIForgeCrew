#!/usr/bin/env bash
# scripts/hermes-configure.sh — configure Hermes to route by role:
#   - Tester + Sr Developer + Sr Architect → LM Studio :1234 (local MLX)
#   - EM → cloud (Anthropic via API key in ~/.hermes/.env)
#
# Writes:
#   ~/.hermes/config.yaml           — default provider = lmstudio
#   ~/.hermes/profiles/em.yaml      — overrides EM to cloud
#   ~/.hermes/profiles/tester.yaml      — local, model=glm-4.7-flash
#   ~/.hermes/profiles/sr-developer.yaml — local, model=qwen3.6-35b-a3b
#   ~/.hermes/profiles/sr-architect.yaml — local, model=gemma-4-31b-it
#
# Paperclip's hermes_local adapter picks the right profile per agent via
# the --profile flag the adapter passes on each spawn.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "hermes-configure: macOS only" >&2; exit 1
fi

HERMES_DIR="${HERMES_DIR:-$HOME/.hermes}"
LLM_ENDPOINT="${LLM_ENDPOINT:-http://localhost:1234/v1}"

mkdir -p "$HERMES_DIR/profiles"

# Default config — local LM Studio.
cat > "$HERMES_DIR/config.yaml" <<EOF
# ~/.hermes/config.yaml — written by scripts/hermes-configure.sh
model:
  default: "qwen3.6-35b-a3b"
  provider: "lmstudio"
  base_url: "$LLM_ENDPOINT"
EOF

cat > "$HERMES_DIR/profiles/em.yaml" <<'EOF'
# EM — cloud only (DESIGN §5.1)
model:
  default: "anthropic/claude-opus-4.6"
  provider: "anthropic"
  # ANTHROPIC_API_KEY must be set in ~/.hermes/.env
EOF

cat > "$HERMES_DIR/profiles/tester.yaml" <<EOF
model:
  default: "zai-org/glm-4.7-flash"
  provider: "lmstudio"
  base_url: "$LLM_ENDPOINT"
EOF

cat > "$HERMES_DIR/profiles/sr-developer.yaml" <<EOF
model:
  default: "qwen3.6-35b-a3b"
  provider: "lmstudio"
  base_url: "$LLM_ENDPOINT"
EOF

cat > "$HERMES_DIR/profiles/sr-architect.yaml" <<EOF
model:
  default: "gemma-4-31b-it"
  provider: "lmstudio"
  base_url: "$LLM_ENDPOINT"
EOF

# .env stub — user fills ANTHROPIC_API_KEY etc.
if [[ ! -f "$HERMES_DIR/.env" ]]; then
  cat > "$HERMES_DIR/.env" <<'EOF'
# Hermes environment. Fill in the ones you need.
# ANTHROPIC_API_KEY=sk-ant-...
# OPENROUTER_API_KEY=sk-or-...
EOF
  echo "created $HERMES_DIR/.env (fill cloud keys for EM role)"
fi

echo
echo "Hermes configured:"
echo "  default       = $LLM_ENDPOINT  qwen3.6-35b-a3b"
echo "  profiles      = em (cloud) / tester / sr-developer / sr-architect"
echo
echo "Smoke:"
echo "  hermes --profile sr-developer --model qwen3.6-35b-a3b -q 'ping'"
