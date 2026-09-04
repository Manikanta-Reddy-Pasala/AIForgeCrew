#!/usr/bin/env bash
# Install CodeGraph — the local code-graph indexer the Doer's codegraph_* tool
# calls are ENFORCED against (runtime/text_doer._CODEGRAPH_MANDATE). It's the npm
# package @colbymchenry/codegraph: SQLite + tree-sitter + FTS5, no vector DB.
#
# No sudo — installs into the npm USER prefix (~/.npm-global by default), so it
# works on a locked-down box. Idempotent; safe to re-run. run.sh calls this
# best-effort on boot; you can also run it by hand once per machine.
set -euo pipefail
PKG="@colbymchenry/codegraph"

if command -v codegraph >/dev/null 2>&1; then
  echo "codegraph already installed: $(command -v codegraph)"
  codegraph --version 2>/dev/null || true
  exit 0
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm not found. Install Node.js/npm first, e.g.:" >&2
  echo "  Debian/Ubuntu:  sudo apt-get install -y nodejs npm" >&2
  echo "  macOS:          brew install node" >&2
  echo "  or nvm:         https://github.com/nvm-sh/nvm" >&2
  exit 1
fi

# Ensure a user-writable global prefix so `npm -g` needs no sudo.
PREFIX="$(npm config get prefix 2>/dev/null || true)"
case "$PREFIX" in
  ""|"/usr"|"/usr/local")
    PREFIX="$HOME/.npm-global"
    npm config set prefix "$PREFIX" >/dev/null 2>&1 || true
    ;;
  # Any other prefix is already user-writable — leave the operator's choice alone.
  *) ;;
esac

echo "==> installing $PKG (npm global → $PREFIX) …"
# --ignore-scripts: a global CLI does not need its postinstall here
# (verified: the package installs and exposes its bin without it), and
# an install script from the registry is code we did not review.
npm install -g --ignore-scripts "$PKG" >/dev/null 2>&1 \
  || npm install -g --ignore-scripts "$PKG"

BIN="$PREFIX/bin/codegraph"
if [[ ! -x "$BIN" ]] && command -v codegraph >/dev/null 2>&1; then
  BIN="$(command -v codegraph)"
fi
if [[ -x "$BIN" ]]; then
  echo "==> installed: $BIN"
  "$BIN" --version 2>/dev/null || true
  echo ""
  echo "Make it discoverable + index your repos:"
  echo "  export PATH=\"$PREFIX/bin:\$PATH\"          # add to ~/.bashrc"
  echo "  # or:  export AIFORGE_CODEGRAPH_BIN=\"$BIN\""
  echo "  export AIFORGE_CODEGRAPH_REPOS=\"/path/repoA,/path/repoB\""
  echo "  ./run.sh                                    # indexes them in the background"
else
  echo "==> install ran but the binary wasn't found — check: npm ls -g $PKG" >&2
  exit 1
fi
