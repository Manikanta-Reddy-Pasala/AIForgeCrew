#!/usr/bin/env bash
set -euo pipefail

SIDECAR_DIR="$(cd "$(dirname "$0")/.." && pwd)/services/rerank_sidecar"
VENV="${RERANK_VENV:-$HOME/.aiforge/venv-rerank}"

mkdir -p "$(dirname "$VENV")"
if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q -r "$SIDECAR_DIR/requirements.txt"

cd "$SIDECAR_DIR"
exec "$VENV/bin/uvicorn" app:app --host 127.0.0.1 --port 8765
