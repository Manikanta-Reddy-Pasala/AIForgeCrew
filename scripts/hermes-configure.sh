#!/usr/bin/env bash
# scripts/hermes-configure.sh — configure Hermes per-role providers.
#
# Defaults:
#   tester / sr-developer / sr-architect  →  LM Studio :1234 (local MLX)
#   em                                     →  NVIDIA NIM (if NVIDIA_API_KEY set)
#                                              or Claude Code (if user ran
#                                              `hermes claude-login`)
#                                              or Anthropic API (ANTHROPIC_API_KEY)
#
# Override via env:
#   EM_PROVIDER=nvidia|claude-code|anthropic|openrouter
#   EM_MODEL=<model-id-for-that-provider>
#   NVIDIA_API_KEY=...   (persisted to ~/.hermes/.env)
#   ANTHROPIC_API_KEY=...
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "hermes-configure: macOS only" >&2; exit 1
fi

HERMES_DIR="${HERMES_DIR:-$HOME/.hermes}"
LLM_ENDPOINT="${LLM_ENDPOINT:-http://localhost:1234/v1}"
EM_PROVIDER="${EM_PROVIDER:-}"
EM_MODEL="${EM_MODEL:-}"

mkdir -p "$HERMES_DIR/profiles"

# Choose EM provider if caller didn't force one.
if [[ -z "$EM_PROVIDER" ]]; then
  if [[ -n "${NVIDIA_API_KEY:-}" ]]; then
    EM_PROVIDER=nvidia
  elif [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    EM_PROVIDER=anthropic
  else
    EM_PROVIDER=claude-code
  fi
fi

# Default model per provider.
if [[ -z "$EM_MODEL" ]]; then
  case "$EM_PROVIDER" in
    nvidia)       EM_MODEL="nvidia/llama-3.3-nemotron-super-49b-v1" ;;
    anthropic)    EM_MODEL="anthropic/claude-opus-4-7" ;;
    claude-code)  EM_MODEL="claude-opus-4-7" ;;
    openrouter)   EM_MODEL="anthropic/claude-opus-4-7" ;;
    *) echo "unknown EM_PROVIDER=$EM_PROVIDER" >&2; exit 2 ;;
  esac
fi

# Default shared config — local LM Studio for everything but EM.
cat > "$HERMES_DIR/config.yaml" <<EOF
# ~/.hermes/config.yaml — written by scripts/hermes-configure.sh
model:
  default: "qwen3.6-35b-a3b"
  provider: "lmstudio"
  base_url: "$LLM_ENDPOINT"
EOF

# EM profile per chosen provider.
case "$EM_PROVIDER" in
  nvidia)
    cat > "$HERMES_DIR/profiles/em.yaml" <<EOF
# EM — NVIDIA NIM. DESIGN §3.1: cloud only, sees ticket text never code.
model:
  default: "$EM_MODEL"
  provider: "nvidia"
  base_url: "https://integrate.api.nvidia.com/v1"
EOF
    ;;
  anthropic)
    cat > "$HERMES_DIR/profiles/em.yaml" <<EOF
# EM — direct Anthropic API.
model:
  default: "$EM_MODEL"
  provider: "anthropic"
EOF
    ;;
  claude-code)
    cat > "$HERMES_DIR/profiles/em.yaml" <<EOF
# EM — Claude Code subscription (no API key; OAuth via `hermes claude-login`).
model:
  default: "$EM_MODEL"
  provider: "openai-codex"
# Run this once on the Mac Studio:
#   hermes claude-login
EOF
    ;;
  openrouter)
    cat > "$HERMES_DIR/profiles/em.yaml" <<EOF
# EM — OpenRouter (single key, many models).
model:
  default: "$EM_MODEL"
  provider: "openrouter"
EOF
    ;;
esac

# Local profiles use the matching LM Studio model id.
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

# Persist API keys into ~/.hermes/.env if caller exported them.
ENV="$HERMES_DIR/.env"
touch "$ENV"; chmod 600 "$ENV"
write_env() {
  local k="$1" v="$2"
  grep -q "^${k}=" "$ENV" && sed -i '' "s|^${k}=.*|${k}=${v}|" "$ENV" \
                         || echo "${k}=${v}" >> "$ENV"
}
[[ -n "${NVIDIA_API_KEY:-}"   ]] && write_env NVIDIA_API_KEY   "$NVIDIA_API_KEY"
[[ -n "${ANTHROPIC_API_KEY:-}" ]] && write_env ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY"
[[ -n "${OPENROUTER_API_KEY:-}" ]] && write_env OPENROUTER_API_KEY "$OPENROUTER_API_KEY"

echo
echo "Hermes configured:"
echo "  default       = $LLM_ENDPOINT  qwen3.6-35b-a3b"
echo "  em            = $EM_PROVIDER    $EM_MODEL"
echo "  tester        = lmstudio        zai-org/glm-4.7-flash"
echo "  sr-developer  = lmstudio        qwen3.6-35b-a3b"
echo "  sr-architect  = lmstudio        gemma-4-31b-it"
echo
if [[ "$EM_PROVIDER" == "claude-code" ]]; then
  echo "NEXT: run once on the Mac Studio to authorize EM via Claude Code subscription:"
  echo "  hermes claude-login"
fi
