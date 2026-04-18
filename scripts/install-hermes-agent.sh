#!/usr/bin/env bash
# scripts/install-hermes-agent.sh — install real Hermes Agent
# (github.com/NousResearch/hermes-agent, Apache-2.0).
#
# Hermes is Python (uv-managed). Official one-liner curl+bash.
# CLI lands at ~/.local/bin/hermes or wherever the installer places it.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install-hermes-agent: macOS only" >&2
  exit 1
fi

# uv is our Python bootstrap — Hermes's installer uses it too.
UV="${UV:-$HOME/.local/bin/uv}"
command -v "$UV" >/dev/null || UV=uv
if ! command -v "$UV" >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Add ~/.local/bin to PATH for this session so `hermes` is reachable below.
export PATH="$HOME/.local/bin:$PATH"

if command -v hermes >/dev/null; then
  echo "[skip] hermes already installed: $(hermes --version 2>&1 | head -1)"
else
  echo ">>> running official Hermes installer"
  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
fi

# Idempotent skills dir.
mkdir -p "$HOME/.hermes/skills"

# Verify.
hash -r
if ! command -v hermes >/dev/null; then
  echo "hermes CLI not on PATH after install. Add \$HOME/.local/bin to PATH." >&2
  exit 1
fi
hermes --version || true
echo
echo "hermes installed. Skills dir: ~/.hermes/skills/"
echo "Next: make hermes-adapter-install"
