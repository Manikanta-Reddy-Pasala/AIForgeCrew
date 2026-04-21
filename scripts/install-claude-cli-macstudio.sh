#!/usr/bin/env bash
# Install Anthropic's Claude CLI globally via Homebrew Node.
# Used by the architect role (transport=claude_cli).
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS only" >&2; exit 1; }

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
command -v npm >/dev/null || { echo "npm missing — install node via: brew install node" >&2; exit 1; }

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
