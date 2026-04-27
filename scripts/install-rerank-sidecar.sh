#!/usr/bin/env bash
# Install + start bge-reranker-v2-m3 ONNX sidecar on :8765.
# Idempotent. Mirror of install-embed-sidecar.sh shape.
set -euo pipefail

VENV="${HOME}/.aiforge/venv-rerank"
MODEL_DIR="${HOME}/.aiforge/models/bge-reranker-v2-m3"
SVC_DIR="$(cd "$(dirname "$0")/.." && pwd)/services/rerank_sidecar"
NO_RUN="0"
[[ "${1:-}" == "--no-run" ]] && NO_RUN="1"

echo "[rerank] venv: $VENV"
# Use python3 -m venv (always ships pip) — uv venv skips pip by default
# which broke the next pip install step on NUC (Linux, no uv installed).
[[ -x "$VENV/bin/pip" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip wheel
"$VENV/bin/pip" install --quiet -r "$SVC_DIR/requirements.txt"

if [[ ! -d "$MODEL_DIR" ]] || [[ ! -f "$MODEL_DIR/config.json" ]]; then
  echo "[rerank] downloading BAAI/bge-reranker-v2-m3 (~568MB PyTorch)…"
  mkdir -p "$MODEL_DIR"
  "$VENV/bin/python" - <<PY
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="BAAI/bge-reranker-v2-m3",
    local_dir="$MODEL_DIR",
)
PY
fi

[[ "$NO_RUN" == "1" ]] && exit 0

echo "[rerank] starting on :8765 (foreground; Ctrl-C to stop)"
cd "$SVC_DIR"
exec "$VENV/bin/uvicorn" app:app --host 127.0.0.1 --port 8765
