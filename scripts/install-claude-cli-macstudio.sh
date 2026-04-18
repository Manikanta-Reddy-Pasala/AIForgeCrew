#!/usr/bin/env bash
# scripts/install-claude-cli-macstudio.sh — install Anthropic's Claude CLI on the Mac Studio.
# Uses Hermes's bundled Node 22 so we don't need separate Node setup.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install-claude-cli: macOS only" >&2; exit 1
fi

export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"
command -v npm >/dev/null || { echo "npm missing — run: make hermes-install first" >&2; exit 1; }

if command -v claude >/dev/null; then
  echo "[skip] claude already installed: $(claude --version 2>&1 | head -1)"
else
  echo ">>> installing @anthropic-ai/claude-code globally"
  npm install -g @anthropic-ai/claude-code
fi

claude --version
echo
echo "Claude CLI installed. Log in with:"
echo "  claude /login        # OAuth with your Claude.ai subscription"
