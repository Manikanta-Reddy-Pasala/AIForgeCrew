#!/usr/bin/env bash
# Install Graphify CLI + build knowledge graph for the repo.
# Prefer uv (Python 3.12). Fall back to pipx with absolute path if uv absent.
set -euo pipefail

if command -v uv >/dev/null; then
  uv tool install --upgrade --python 3.12 graphifyy
  # uv tool installs put console scripts in ~/.local/bin (macOS default).
  export PATH="$HOME/.local/bin:$PATH"
else
  if ! command -v pipx >/dev/null; then
    echo "installing pipx first..."
    python3 -m pip install --user pipx
  fi
  PIPX_BIN="$(command -v pipx || true)"
  [[ -z "$PIPX_BIN" ]] && PIPX_BIN="$HOME/Library/Python/3.9/bin/pipx"
  "$PIPX_BIN" install graphifyy || "$PIPX_BIN" upgrade graphifyy
  export PATH="$HOME/.local/bin:$PATH"
fi

# Sets up slash commands / MCP server for the user's coding assistants.
graphify install

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "extracting code graph (AST pass, no LLM)..."
graphify update .

echo "Graphify graph ready. Top insights: graphify-out/GRAPH_REPORT.md"
echo "For semantic enrichment, use /graphify . inside Claude Code / your AI assistant."
