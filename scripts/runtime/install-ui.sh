#!/usr/bin/env bash
# Provision the web UI (FastAPI + Vite/React) on Mac Studio.
# Runs after install.sh. Idempotent.
set -euo pipefail

REPO="${REPO:-$HOME/AIForgeCrew}"
VENV="$REPO/.venv"
UV=/opt/homebrew/bin/uv

[[ -d "$REPO" ]] || { echo "no $REPO" >&2; exit 1; }

echo ">>> 1/4 install backend deps (fastapi + uvicorn + pydantic already via openai)"
"$UV" pip install --python "$VENV/bin/python" \
  'fastapi>=0.115' 'uvicorn[standard]>=0.32' 'pydantic>=2.9' 2>&1 | tail -3

echo ">>> 2/4 install frontend deps (npm)"
cd "$REPO/web"
if command -v npm >/dev/null 2>&1; then
  npm install --no-audit --no-fund --silent
  echo "   npm deps installed"
else
  echo "   !! npm not on PATH — install Node first: brew install node"
fi

echo ">>> 3/4 install API LaunchAgent (com.aiforge.api)"
cp "$REPO/scripts/runtime/com.aiforge.api.plist" \
   "$HOME/Library/LaunchAgents/com.aiforge.api.plist"
launchctl bootout "gui/$(id -u)/com.aiforge.api" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.aiforge.api.plist"

echo ">>> 4/4 dev-build frontend once (optional, for static preview)"
cd "$REPO/web"
if command -v npm >/dev/null 2>&1; then
  npm run build --silent || echo "   (build skipped)"
fi

echo
echo "UI installed."
echo "  API:      http://127.0.0.1:8799/api/health"
echo "  Dev UI:   cd $REPO/web && npm run dev  (http://127.0.0.1:5173)"
echo "  Static:   $REPO/web/dist/"
