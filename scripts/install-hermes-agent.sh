#!/usr/bin/env bash
# scripts/install-hermes-agent.sh — install real Hermes Agent
# (github.com/NousResearch/hermes-agent) on the Mac Studio.
#
# Hermes ships: 30+ native tools, 80+ skills, session persistence, MCP
# client + server mode, multi-provider LLM. CLI: `hermes`.
# Config lives at ~/.hermes/ (skills, sessions, memory).
#
# Node 20+ required. fnm bootstrap reused from install-paperclip-ui.sh
# (run that first if Node is missing).
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install-hermes-agent: macOS only" >&2
  exit 1
fi

# Node 20+ check (fnm should already be installed by install-paperclip-ui.sh)
if ! command -v node >/dev/null || [[ "$(node -v | sed 's/v//' | cut -d. -f1)" -lt 20 ]]; then
  FNM_BIN="$HOME/Library/Application Support/fnm/fnm"
  if [[ -x "$FNM_BIN" ]]; then
    export FNM_DIR="$HOME/.fnm"
    mkdir -p "$FNM_DIR"
    eval "$("$FNM_BIN" env --shell bash)"
    "$FNM_BIN" use 20 2>/dev/null || "$FNM_BIN" install 20
  else
    echo "Node 20 missing. Run `make paperclip-install` first (bootstraps fnm+Node)." >&2
    exit 1
  fi
fi

if command -v hermes >/dev/null; then
  echo "[skip] hermes already installed: $(hermes --version 2>&1 | head -1)"
else
  echo ">>> installing hermes-agent via npm"
  npm install -g @nousresearch/hermes-agent
fi

# Idempotent init. Hermes creates ~/.hermes on first run.
mkdir -p "$HOME/.hermes/skills"
hermes --version
echo
echo "hermes installed. Skills dir: ~/.hermes/skills/"
echo "Next: make hermes-adapter-install (wires Paperclip hermes_local adapter)"
