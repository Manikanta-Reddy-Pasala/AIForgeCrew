#!/usr/bin/env bash
# scripts/deploy-to-mac-studio.sh — clone/pull the repo on the Mac Studio + install aiforge.
# Runs remotely. Idempotent.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew}"
REPO_DIR="${REPO_DIR:-$HOME/AIForgeCrew}"

if [[ -d "$REPO_DIR/.git" ]]; then
  echo ">>> pulling $REPO_DIR"
  git -C "$REPO_DIR" fetch origin
  git -C "$REPO_DIR" reset --hard origin/main
else
  echo ">>> cloning $REPO_URL → $REPO_DIR"
  git clone "$REPO_URL" "$REPO_DIR"
fi

cd "$REPO_DIR"
bash scripts/install-aiforge.sh
echo
echo "aiforge deployed to $REPO_DIR"
"$REPO_DIR/.venv/bin/aiforge" --version
