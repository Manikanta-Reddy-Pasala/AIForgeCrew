#!/usr/bin/env bash
# scripts/install-hermes-adapter.sh — install hermes-paperclip-adapter.
# Lets Paperclip's hermes_local adapter shell into the Hermes CLI.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install-hermes-adapter: macOS only" >&2; exit 1
fi

# Hermes bundles its own Node 22 at ~/.hermes/node; reuse it.
export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"

command -v hermes >/dev/null || { echo "hermes CLI missing — run: make hermes-install" >&2; exit 1; }
command -v npm    >/dev/null || { echo "npm missing — reinstall Hermes to get bundled Node" >&2; exit 1; }

if npm list -g hermes-paperclip-adapter >/dev/null 2>&1; then
  echo "[skip] hermes-paperclip-adapter already installed"
else
  echo ">>> installing hermes-paperclip-adapter"
  npm install -g hermes-paperclip-adapter
fi

# Check adapter visibility in Paperclip.
if curl -s -o /dev/null -f http://localhost:3100/api/health; then
  echo
  echo "Paperclip up — probing hermes_local adapter"
  for cid in $(curl -s http://localhost:3100/api/companies | jq -r '.[].id'); do
    echo "  company=$cid"
    curl -s "http://localhost:3100/api/companies/$cid/adapters/hermes_local/models" | head -c 400
    echo
  done
fi

echo
echo "Adapter installed. Agents with adapterType=hermes_local now dispatch via the local hermes CLI."
