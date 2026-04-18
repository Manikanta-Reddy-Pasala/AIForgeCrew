#!/usr/bin/env bash
# scripts/hermes-configure.sh — configure Hermes per-role providers + fallbacks.
#
# Defaults:
#   tester / sr-developer / sr-architect  →  LM Studio :1234 (local MLX)
#   em                                     →  Claude Code subscription
#                                              (set ANTHROPIC_API_KEY for direct API,
#                                               or EM_PROVIDER=nvidia to override)
#   <role>-fallback                         →  NVIDIA NIM with minimax-m2.7 (230B)
#                                              (requires NVIDIA_API_KEY)
#
# Overrides (env vars):
#   EM_PROVIDER=nvidia|claude-code|anthropic|openrouter
#   EM_MODEL=<id>
#   FALLBACK_PROVIDER=nvidia|openrouter
#   FALLBACK_MODEL=<id>  (default: minimax-ai/minimax-m2.7)
#   NVIDIA_API_KEY=...   → persisted to ~/.hermes/.env (chmod 600)
#   ANTHROPIC_API_KEY=... / OPENROUTER_API_KEY=...
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "hermes-configure: macOS only" >&2; exit 1; }

HERMES_DIR="${HERMES_DIR:-$HOME/.hermes}"
LLM_ENDPOINT="${LLM_ENDPOINT:-http://localhost:1234/v1}"
EM_PROVIDER="${EM_PROVIDER:-}"
EM_MODEL="${EM_MODEL:-}"
FALLBACK_PROVIDER="${FALLBACK_PROVIDER:-nvidia}"
FALLBACK_MODEL="${FALLBACK_MODEL:-minimax-ai/minimax-m2.7}"
FALLBACK_BASE="https://integrate.api.nvidia.com/v1"

mkdir -p "$HERMES_DIR/profiles"

# Pick EM provider.
# Preference order: explicit EM_PROVIDER > ANTHROPIC_API_KEY > NVIDIA_API_KEY > openai-codex OAuth.
# (Hermes's `login` subcommand supports only `nous` and `openai-codex` OAuth paths —
#  there is no direct Claude Code / anthropic OAuth flow, so ANTHROPIC_API_KEY is the
#  only way to hit Claude directly.)
if [[ -z "$EM_PROVIDER" ]]; then
  if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then EM_PROVIDER=anthropic
  elif [[ -n "${NVIDIA_API_KEY:-}" ]];   then EM_PROVIDER=nvidia
  else EM_PROVIDER=openai-codex
  fi
fi
if [[ -z "$EM_MODEL" ]]; then
  case "$EM_PROVIDER" in
    nvidia)        EM_MODEL="nvidia/llama-3.3-nemotron-super-49b-v1" ;;
    anthropic)     EM_MODEL="anthropic/claude-opus-4-7" ;;
    openai-codex)  EM_MODEL="gpt-5-codex" ;;
    openrouter)    EM_MODEL="anthropic/claude-opus-4-7" ;;
    *) echo "unknown EM_PROVIDER=$EM_PROVIDER" >&2; exit 2 ;;
  esac
fi

# Shared default — local LM Studio.
cat > "$HERMES_DIR/config.yaml" <<EOF
# ~/.hermes/config.yaml — written by scripts/hermes-configure.sh
model:
  default: "qwen3.6-35b-a3b"
  provider: "lmstudio"
  base_url: "$LLM_ENDPOINT"
EOF

# EM profile.
case "$EM_PROVIDER" in
  nvidia)
    cat > "$HERMES_DIR/profiles/em.yaml" <<EOF
# EM — NVIDIA NIM. DESIGN §3.1: cloud only, sees ticket text never code.
model:
  default: "$EM_MODEL"
  provider: "nvidia"
  base_url: "$FALLBACK_BASE"
EOF
    ;;
  anthropic)
    cat > "$HERMES_DIR/profiles/em.yaml" <<EOF
model:
  default: "$EM_MODEL"
  provider: "anthropic"
EOF
    ;;
  openai-codex)
    cat > "$HERMES_DIR/profiles/em.yaml" <<EOF
# EM — OpenAI Codex CLI (ChatGPT Plus subscription via OAuth).
# Run once on the Mac Studio:  hermes login --provider openai-codex
model:
  default: "$EM_MODEL"
  provider: "openai-codex"
EOF
    ;;
  openrouter)
    cat > "$HERMES_DIR/profiles/em.yaml" <<EOF
model:
  default: "$EM_MODEL"
  provider: "openrouter"
EOF
    ;;
esac

# Local role profiles (first-pass).
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

# Fallback profiles — used after ≥2 local failures on the same ticket.
# Paperclip's hermes_local adapter switches profile via --profile <role>-fallback
# when aiforge_core.retry.should_escalate_to_fallback() returns True.
write_fallback() {
  local role="$1"
  cat > "$HERMES_DIR/profiles/${role}-fallback.yaml" <<EOF
# ${role} fallback — big cloud model when local MLX gets stuck (DESIGN §10 retry rules).
model:
  default: "$FALLBACK_MODEL"
  provider: "$FALLBACK_PROVIDER"
  base_url: "$FALLBACK_BASE"
EOF
}
write_fallback tester
write_fallback sr-developer
write_fallback sr-architect

# Persist keys into ~/.hermes/.env (chmod 600).
ENV="$HERMES_DIR/.env"
touch "$ENV"; chmod 600 "$ENV"
write_env() {
  local k="$1" v="$2"
  grep -q "^${k}=" "$ENV" && sed -i '' "s|^${k}=.*|${k}=${v}|" "$ENV" \
                         || echo "${k}=${v}" >> "$ENV"
}
[[ -n "${NVIDIA_API_KEY:-}"    ]] && write_env NVIDIA_API_KEY    "$NVIDIA_API_KEY"
[[ -n "${ANTHROPIC_API_KEY:-}" ]] && write_env ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY"
[[ -n "${OPENROUTER_API_KEY:-}" ]] && write_env OPENROUTER_API_KEY "$OPENROUTER_API_KEY"

echo
echo "Hermes configured:"
echo "  default       local LM Studio :1234"
echo "  em            $EM_PROVIDER    $EM_MODEL"
echo "  tester        lmstudio        zai-org/glm-4.7-flash"
echo "  sr-developer  lmstudio        qwen3.6-35b-a3b"
echo "  sr-architect  lmstudio        gemma-4-31b-it"
echo "  *-fallback    $FALLBACK_PROVIDER  $FALLBACK_MODEL"
echo
case "$EM_PROVIDER" in
  openai-codex)
    echo "NEXT: authorize EM (one-time, OAuth):"
    echo "  hermes login --provider openai-codex"
    ;;
  nvidia)
    [[ -n "${NVIDIA_API_KEY:-}" ]] \
      && echo "NVIDIA_API_KEY persisted to ~/.hermes/.env — EM ready." \
      || echo "WARN: EM_PROVIDER=nvidia but NVIDIA_API_KEY not set. Re-run with the key exported."
    ;;
  anthropic)
    [[ -n "${ANTHROPIC_API_KEY:-}" ]] \
      && echo "ANTHROPIC_API_KEY persisted to ~/.hermes/.env — EM ready." \
      || echo "WARN: EM_PROVIDER=anthropic but ANTHROPIC_API_KEY not set."
    ;;
esac
