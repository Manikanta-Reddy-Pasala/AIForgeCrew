#!/usr/bin/env bash
# scripts/install-paperclip-ui.sh — install real Paperclip (Node.js + React UI)
# on the Mac Studio. Trusted-loopback mode by default (no LAN exposure).
# Source: https://github.com/paperclipai/paperclip  (MIT)
#
# Bootstraps Node 20 via fnm if missing (single binary, no brew / no sudo).
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "paperclip UI install: macOS only" >&2
  exit 1
fi

# --- Node 20+ via fnm ---
if ! command -v node >/dev/null || [[ "$(node -v | sed 's/v//' | cut -d. -f1)" -lt 20 ]]; then
  if ! command -v fnm >/dev/null; then
    echo ">>> installing fnm (Fast Node Manager)"
    # --force-install bypasses Homebrew detection (Mac Studio has no brew)
    curl -fsSL https://fnm.vercel.app/install | bash -s -- --skip-shell --force-install
  fi
  # fnm installs to ~/Library/Application Support/fnm/fnm on macOS.
  FNM_BIN="$HOME/Library/Application Support/fnm/fnm"
  [[ -x "$FNM_BIN" ]] || FNM_BIN="$(command -v fnm || true)"
  export FNM_DIR="$HOME/.fnm"
  mkdir -p "$FNM_DIR"
  eval "$("$FNM_BIN" env --shell bash)"
  "$FNM_BIN" install 20 >/dev/null
  "$FNM_BIN" use 20
  # Ensure node/npx are picked up by later commands in this script.
  hash -r
fi

if ! command -v node >/dev/null; then
  echo "Node install failed — please add $HOME/.local/share/fnm to PATH and retry" >&2
  exit 1
fi

if ! command -v pnpm >/dev/null; then
  echo ">>> installing pnpm"
  npm install -g pnpm@latest
fi

# Idempotent onboard.
if [[ ! -d "$HOME/.paperclip" ]]; then
  echo ">>> running: npx paperclipai onboard --yes"
  npx -y paperclipai onboard --yes
else
  echo "[skip] Paperclip already onboarded at ~/.paperclip"
fi

echo
echo "Paperclip installed. Start with: make paperclip-start"
echo "UI will be at http://localhost:3100 (trusted-loopback mode)"
echo
echo "To reach the UI from your laptop, tunnel:"
echo "  ssh -L 3100:localhost:3100 manikanta@<mac-studio-ip>"
echo "  open http://localhost:3100 on your laptop"
