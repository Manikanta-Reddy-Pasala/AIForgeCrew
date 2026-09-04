#!/usr/bin/env bash
# build-ui: build the Vite/React dashboard + restart aiforge-api.
#
# FastAPI mounts web/dist at /ui/ when it exists (see aiforge_core/runtime/
# api.py line ~480). NUC ships the sources but the build artifact is
# git-ignored, so a fresh deploy needs an explicit build.
#
# Run on NUC:
#   bash scripts/runtime/nuc/build-ui.sh
set -euo pipefail

REPO="${REPO:-$HOME/AIForgeCrew}"
cd "$REPO/web"

if ! command -v npm >/dev/null; then
  echo "npm not found — apt-get install -y nodejs npm" >&2
  exit 2
fi

npm install --ignore-scripts --no-audit --no-fund --silent
npm run build

# Bounce the user-mode API so it sees the fresh dist/.
systemctl --user restart aiforge-api
sleep 2
systemctl --user is-active aiforge-api

echo "UI built. Open https://77.42.45.12:9443/ui/ (basic-auth)."
