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
[[ -x "$VENV/bin/python" ]] || /opt/homebrew/bin/uv venv "$VENV" --python 3.12 || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip wheel
"$VENV/bin/pip" install --quiet -r "$SVC_DIR/requirements.txt"

if [[ ! -f "$MODEL_DIR/model.onnx" ]]; then
  echo "[rerank] downloading bge-reranker-v2-m3 ONNX (~600MB)…"
  mkdir -p "$MODEL_DIR"
  "$VENV/bin/python" - <<PY
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="BAAI/bge-reranker-v2-m3",
    allow_patterns=["onnx/model.onnx", "tokenizer*", "special_tokens_map.json", "sentencepiece.bpe.model"],
    local_dir="$MODEL_DIR",
)
import os, shutil
src = os.path.join("$MODEL_DIR", "onnx", "model.onnx")
dst = os.path.join("$MODEL_DIR", "model.onnx")
if os.path.isfile(src) and not os.path.isfile(dst):
    shutil.move(src, dst)
PY
fi

[[ "$NO_RUN" == "1" ]] && exit 0

echo "[rerank] starting on :8765 (foreground; Ctrl-C to stop)"
cd "$SVC_DIR"
exec "$VENV/bin/uvicorn" app:app --host 127.0.0.1 --port 8765
