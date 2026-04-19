#!/usr/bin/env bash
# scripts/install-brew-macstudio.sh — install Homebrew on Mac Studio +
# openssl@3 + postgresql@16. Required for:
#   - Hindsight's embedded pg0 daemon (links against /opt/homebrew openssl@3)
#   - our pgvector setup (postgresql@16)
#
# Requires passwordless sudo (see README > passwordless sudo section).
# Idempotent.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "install-brew: macOS only" >&2; exit 1; }

# ---- 1. Xcode Command Line Tools ----
if ! xcode-select -p >/dev/null 2>&1; then
  echo ">>> Xcode CLT missing — install manually: xcode-select --install" >&2
  exit 1
fi
echo "Xcode CLT: $(xcode-select -p)"

# ---- 2. Homebrew ----
if ! command -v brew >/dev/null && [[ ! -x /opt/homebrew/bin/brew ]]; then
  echo ">>> installing Homebrew (non-interactive)"
  NONINTERACTIVE=1 /bin/bash -c \
    "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# Put brew on PATH for this run.
if [[ -x /opt/homebrew/bin/brew ]]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi
brew --version

# ---- 3. Formulae we need ----
need() {
  local f="$1"
  if brew list --formula "$f" >/dev/null 2>&1; then
    echo "  [skip]   $f already installed"
  else
    echo "  [install] $f"
    brew install "$f"
  fi
}

need openssl@3
need postgresql@16
need pgvector

# Verify the dylib Hindsight's pg0 needs.
DYLIB="/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib"
if [[ -f "$DYLIB" ]]; then
  echo "openssl@3 dylib present: $DYLIB"
else
  echo "FAIL: openssl@3 dylib missing at $DYLIB" >&2; exit 1
fi

# ---- 4. Start Postgres ----
if ! brew services list | grep -q "^postgresql@16.*started"; then
  brew services start postgresql@16
fi

echo
echo "Homebrew + openssl@3 + postgresql@16 ready."
echo "Next: scripts/hermes-setup-hindsight.sh"
