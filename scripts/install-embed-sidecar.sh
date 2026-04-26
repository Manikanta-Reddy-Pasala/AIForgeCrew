#!/usr/bin/env bash
# Install + run bge-m3 embed sidecar on port 8764. Uses uv + Python 3.12.
set -euo pipefail

SIDECAR_DIR="$(cd "$(dirname "$0")/.." && pwd)/services/embed_sidecar"
MODEL_DIR="${BGE_M3_DIR:-$HOME/.aiforge/models/bge-m3}"
VENV="${EMBED_VENV:-$HOME/.aiforge/venv-embed}"

mkdir -p "$MODEL_DIR" "$(dirname "$VENV")"

if ! command -v uv >/dev/null; then
  echo "uv required. Install via: brew install uv" >&2
  exit 1
fi

if [[ ! -d "$VENV" ]]; then
  uv venv --python 3.12 "$VENV"
fi
uv pip install --python "$VENV/bin/python" -r "$SIDECAR_DIR/requirements.txt"

# Model download (ONNX export of bge-m3) — first run only
if [[ ! -f "$MODEL_DIR/model.onnx" ]]; then
  "$VENV/bin/python" -c "
from huggingface_hub import snapshot_download
snapshot_download('aapot/bge-m3-onnx', local_dir='$MODEL_DIR')
"
fi

cd "$SIDECAR_DIR"
# --no-run = setup only (used by systemd ExecStartPre). Default
# behaviour kept (interactive shell run) for legacy callers.
if [[ "${1:-}" == "--no-run" ]]; then
  echo "[install-embed-sidecar] setup done; not exec'ing uvicorn (--no-run)"
  exit 0
fi
exec "$VENV/bin/uvicorn" app:app --host 127.0.0.1 --port 8764
