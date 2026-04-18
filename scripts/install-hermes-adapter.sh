#!/usr/bin/env bash
# scripts/install-hermes-adapter.sh — install hermes-paperclip-adapter
# (github.com/NousResearch/hermes-paperclip-adapter).
#
# The adapter lets Paperclip's `hermes_local` agent type shell to the Hermes
# CLI with --resume session persistence. Paperclip agents we created earlier
# already have adapterType=hermes_local — installing this adapter is what
# makes that wiring actually dispatch.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install-hermes-adapter: macOS only" >&2; exit 1
fi

command -v node >/dev/null || { echo "Node missing — run `make hermes-install` first" >&2; exit 1; }
command -v hermes >/dev/null || { echo "hermes CLI missing — run `make hermes-install` first" >&2; exit 1; }

if npm list -g @nousresearch/hermes-paperclip-adapter >/dev/null 2>&1; then
  echo "[skip] hermes-paperclip-adapter already installed"
else
  echo ">>> installing hermes-paperclip-adapter via npm"
  npm install -g @nousresearch/hermes-paperclip-adapter
fi

# Paperclip auto-detects the adapter in its adapter discovery path.
# Validate by hitting Paperclip's adapter list endpoint.
if curl -s -o /dev/null -f http://localhost:3100/api/health; then
  echo
  echo "Paperclip is up. Checking adapter visibility..."
  for cid in $(curl -s http://localhost:3100/api/companies | jq -r '.[].id'); do
    echo "  company=$cid"
    curl -s "http://localhost:3100/api/companies/$cid/adapters/hermes_local/models" | head -c 300
    echo
  done
fi

echo
echo "Adapter installed. Each Paperclip agent with adapterType=hermes_local"
echo "now routes through the local hermes CLI."
