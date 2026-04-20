#!/usr/bin/env bash
# Install + run bge-m3 embed sidecar on port 8764.
set -euo pipefail

SIDECAR_DIR="$(cd "$(dirname "$0")/.." && pwd)/services/embed_sidecar"
MODEL_DIR="${BGE_M3_DIR:-$HOME/.aiforge/models/bge-m3}"
VENV="${EMBED_VENV:-$HOME/.aiforge/venv-embed}"

mkdir -p "$MODEL_DIR" "$(dirname "$VENV")"

if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q -r "$SIDECAR_DIR/requirements.txt"

# Model download (ONNX export of bge-m3) — first run only
if [[ ! -f "$MODEL_DIR/model.onnx" ]]; then
  "$VENV/bin/python" -c "
from huggingface_hub import snapshot_download
snapshot_download('aapot/bge-m3-onnx', local_dir='$MODEL_DIR', local_dir_use_symlinks=False)
"
fi

cd "$SIDECAR_DIR"
exec "$VENV/bin/uvicorn" app:app --host 127.0.0.1 --port 8764
