#!/usr/bin/env bash
# Install Graphify CLI + build knowledge graph for the repo.
set -euo pipefail

if ! command -v pipx >/dev/null; then
  echo "installing pipx first..."
  python3 -m pip install --user pipx
  python3 -m pipx ensurepath
fi

pipx install graphifyy || pipx upgrade graphifyy

# Sets up slash commands / MCP server for the user's coding assistants.
graphify install

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -d "graphify-out" ]]; then
  echo "building graph for the first time (this may take minutes)..."
  graphify .
else
  echo "rebuilding graph..."
  graphify . --incremental
fi

echo "Graphify graph ready. Top insights: graphify-out/GRAPH_REPORT.md"
