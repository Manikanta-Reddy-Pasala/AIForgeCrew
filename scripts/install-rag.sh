#!/usr/bin/env bash
# scripts/install-rag.sh — ensure chromadb is installed + build RAG index.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install-rag: macOS only" >&2; exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

UV="${UV:-$HOME/.local/bin/uv}"; command -v "$UV" >/dev/null || UV=uv

[[ -d .venv ]] || { echo "run scripts/install-paperclip.sh first" >&2; exit 1; }

echo ">>> installing chromadb into .venv"
"$UV" pip install --python .venv/bin/python chromadb

echo ">>> building RAG index"
.venv/bin/python -c "
from pathlib import Path
from paperclip.rag import RagIndex
idx = RagIndex(Path('.'))
print(idx.reindex())
"
