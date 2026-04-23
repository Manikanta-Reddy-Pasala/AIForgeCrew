#!/usr/bin/env bash
# Invoked by post-merge git hook. Extracts and re-ingests only changed files.
#   Usage: graph_incremental.sh <repo_path> <changed_file_1> [...]
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_PATH="${1:?repo path required}"
shift
FILES=("$@")
REPO_NAME="$(basename "$REPO_PATH")"
NEO4J="${NEO4J_URI:-bolt://127.0.0.1:7687}"
VENV="${VENV:-$HOME/aiforge-venv}"
PY="$VENV/bin/python"
OUT=/tmp/graph_rag/delta-${REPO_NAME}.jsonl

echo "[incremental] repo=$REPO_NAME files=${#FILES[@]}"

# Classify by extension; run appropriate extractor.
JAVA_FILES=(); TS_FILES=(); PY_FILES=(); MD_FILES=()
for f in "${FILES[@]}"; do
  case "$f" in
    *.java)                JAVA_FILES+=("$f") ;;
    *.ts|*.tsx|*.js|*.jsx) TS_FILES+=("$f") ;;
    *.py)                  PY_FILES+=("$f") ;;
    *.md)                  MD_FILES+=("$f") ;;
  esac
done

if [ "${#JAVA_FILES[@]}" -gt 0 ]; then
  java -jar javaparser/target/graph-rag-extractor.jar \
    --repo "$REPO_PATH" --out "$OUT" --files "${JAVA_FILES[@]}" \
    || echo "  javaparser skipped"
  [ -f "$OUT" ] && "$PY" ingest_jsonl.py --jsonl "$OUT" --neo4j "$NEO4J" --delete-scope file || true
fi
if [ "${#TS_FILES[@]}" -gt 0 ] && [ -f tsparser/dist/extractor.js ]; then
  node tsparser/dist/extractor.js --repo "$REPO_PATH" --out "$OUT" --files "${TS_FILES[@]}"
  "$PY" ingest_jsonl.py --jsonl "$OUT" --neo4j "$NEO4J" --delete-scope file || true
fi
if [ "${#PY_FILES[@]}" -gt 0 ]; then
  "$VENV/bin/aiforge-pyparser" --repo "$REPO_PATH" --out "$OUT" --files "${PY_FILES[@]}"
  "$PY" ingest_jsonl.py --jsonl "$OUT" --neo4j "$NEO4J" --delete-scope file || true
fi
if [ "${#MD_FILES[@]}" -gt 0 ]; then
  "$PY" ingest_memory.py --files "${MD_FILES[@]}" --neo4j "$NEO4J"
  "$PY" link_memories.py --neo4j "$NEO4J" --limit "${#MD_FILES[@]}"
fi

"$PY" embed_nodes.py --neo4j "$NEO4J" --only-new --lm "${LM_URL:-http://127.0.0.1:8764}" \
  --model "${EMBED_MODEL:-bge-m3}" --dim "${EMBED_DIM:-1024}" || true

echo "[incremental] done"
