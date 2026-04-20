#!/usr/bin/env bash
# Install + run bge-reranker-v2-m3 rerank sidecar on port 8765. Uses uv + Python 3.12.
set -euo pipefail

SIDECAR_DIR="$(cd "$(dirname "$0")/.." && pwd)/services/rerank_sidecar"
VENV="${RERANK_VENV:-$HOME/.aiforge/venv-rerank}"

mkdir -p "$(dirname "$VENV")"

if ! command -v uv >/dev/null; then
  echo "uv required. Install via: brew install uv" >&2
  exit 1
fi

if [[ ! -d "$VENV" ]]; then
  uv venv --python 3.12 "$VENV"
fi
uv pip install --python "$VENV/bin/python" -r "$SIDECAR_DIR/requirements.txt"

cd "$SIDECAR_DIR"
exec "$VENV/bin/uvicorn" app:app --host 127.0.0.1 --port 8765
