#!/usr/bin/env bash
# scripts/install-paperclip.sh — install paperclip package via uv.
# Mac-only (no Linux/Windows support). Idempotent: safe to re-run.
#
# Creates .venv/ at repo root, installs paperclip + dev deps editable,
# exposes `paperclip` CLI binary under .venv/bin/.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "paperclip install: macOS only (got $(uname -s))" >&2
  exit 1
fi

UV="${UV:-$HOME/.local/bin/uv}"
command -v "$UV" >/dev/null || UV=uv
if ! command -v "$UV" >/dev/null; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VENV=".venv"
if [[ ! -d "$VENV" ]]; then
  "$UV" venv "$VENV"
fi

# Install in editable mode with dev extras.
"$UV" pip install --python "$VENV/bin/python" -e ".[dev]"

# Sanity.
"$VENV/bin/paperclip" --version
echo
echo "paperclip installed. Add to PATH or invoke via ./.venv/bin/paperclip"
