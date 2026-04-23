#!/usr/bin/env bash
# Cold-start: nuke, build extractors, run every lang, ingest, link, embed, sanity.
# Set NO_RESET=1 to skip the nuke phase.
set -euo pipefail

cd "$(dirname "$0")/.."
GR_DIR="$(pwd)"

NEO4J="${NEO4J_URI:-bolt://127.0.0.1:7687}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASS="${NEO4J_PASS:-password}"
REPOS_ROOT="${REPOS_ROOT:-$HOME/Documents/codeRepo}"
VENV="${VENV:-$HOME/aiforge-venv}"
OUT="${OUT:-/tmp/graph_rag}"
NO_RESET="${NO_RESET:-0}"
mkdir -p "$OUT"

PY="$VENV/bin/python"
PIP="$VENV/bin/pip"

[ -d "$VENV" ] || python3 -m venv "$VENV"
"$PIP" install -q -r requirements.txt
"$PIP" install -q -e pyparser
"$PIP" install -q -e mcp_server

echo "==[0/11]== Nuke graph (set NO_RESET=1 to skip)"
if [ "$NO_RESET" != "1" ]; then
  # Single session, wipe nodes + relationships + vector indexes +
  # constraints. bge-m3 dim (1024) differs from v3 nomic (768) so old
  # vector indexes must be dropped to avoid dim mismatch on re-embed.
  "$PY" - <<PY
from neo4j import GraphDatabase
drv = GraphDatabase.driver("$NEO4J", auth=("$NEO4J_USER","$NEO4J_PASS"))
with drv.session() as s:
    for idx in ("method_embedding_vec","class_embedding_vec",
                "endpoint_embedding_vec","memory_embedding_vec",
                "natssubject_embedding_vec","mongocollection_embedding_vec",
                "method_text","memory_text"):
        try: s.run(f"DROP INDEX {idx} IF EXISTS")
        except Exception as e: print("  idx drop skip:", idx, e)
    for c in [r["name"] for r in s.run("SHOW CONSTRAINTS YIELD name")]:
        try: s.run(f"DROP CONSTRAINT \`{c}\`")
        except Exception: pass
    # Delete in chunks to avoid OOM on large graphs.
    while True:
        n = s.run("MATCH (n) WITH n LIMIT 50000 DETACH DELETE n RETURN count(*) AS n").single()["n"]
        print(f"  deleted {n}")
        if n == 0: break
drv.close()
PY
  echo "  nuke complete"
  rm -rf "$OUT"/*.jsonl "$OUT"/*.scip 2>/dev/null || true
fi

echo "==[1/11]== Build Java extractor"
(cd javaparser && mvn -q package -DskipTests)

echo "==[2/11]== Build TS extractor"
(cd tsparser && (command -v npm >/dev/null && npm ci && npm run build) || echo "skip tsparser (no npm)")

echo "==[3/11]== Repo meta"
"$PY" repo_meta.py --root "$REPOS_ROOT" > "$OUT/repo_meta.jsonl"
echo "  repos:  $(wc -l < "$OUT/repo_meta.jsonl")"

echo "==[4/11]== Per-repo AST extract"
while IFS= read -r line; do
  repo=$(echo "$line" | "$PY" -c "import sys,json;print(json.loads(sys.stdin.read())['repo'])")
  lang=$(echo "$line" | "$PY" -c "import sys,json;print(json.loads(sys.stdin.read())['lang'])")
  path=$(echo "$line" | "$PY" -c "import sys,json;print(json.loads(sys.stdin.read())['path'])")
  case "$lang" in
    java)
      java -jar javaparser/target/graph-rag-extractor.jar \
        --repo "$path" --out "$OUT/${repo}.java.jsonl" || echo "  skip $repo"
      ;;
    node|react)
      [ -f tsparser/dist/extractor.js ] && \
        node tsparser/dist/extractor.js --repo "$path" --out "$OUT/${repo}.ts.jsonl" \
        || echo "  skip $repo (no ts build)"
      ;;
    python)
      "$VENV/bin/aiforge-pyparser" --repo "$path" --out "$OUT/${repo}.py.jsonl" \
        || echo "  skip $repo"
      ;;
    *)
      echo "  unsupported lang: $lang ($repo)"
      ;;
  esac
done < "$OUT/repo_meta.jsonl"

echo "==[5/11]== SCIP indexing (optional — skipped if tools missing)"
while IFS= read -r line; do
  repo=$(echo "$line" | "$PY" -c "import sys,json;print(json.loads(sys.stdin.read())['repo'])")
  lang=$(echo "$line" | "$PY" -c "import sys,json;print(json.loads(sys.stdin.read())['lang'])")
  path=$(echo "$line" | "$PY" -c "import sys,json;print(json.loads(sys.stdin.read())['path'])")
  case "$lang" in
    java)   command -v scip-java >/dev/null && (cd "$path" && scip-java index --output "$OUT/${repo}.scip" -- ./mvnw -q package -DskipTests) || true ;;
    node|react) command -v scip-typescript >/dev/null && (cd "$path" && scip-typescript index --infer-tsconfig --output "$OUT/${repo}.scip") || true ;;
    python) command -v scip-python >/dev/null && (cd "$path" && scip-python index . --project-name "$repo" --output "$OUT/${repo}.scip") || true ;;
  esac
  if [ -f "$OUT/${repo}.scip" ]; then
    "$PY" scip_to_neo4j.py --scip "$OUT/${repo}.scip" --repo "$repo" \
      --lang "$lang" --neo4j "$NEO4J" || true
  fi
done < "$OUT/repo_meta.jsonl"

echo "==[6/11]== K8s snapshot (qa + prod)"
"$PY" k8s_sync.py --env qa --kubeconfig "${QA_KUBECONFIG:-$HOME/.kubeconfigs/qa.yaml}" \
  --context "${QA_CONTEXT:-qa}" > "$OUT/k8s-qa.jsonl" 2>/dev/null || echo "  qa skipped"
"$PY" k8s_sync.py --env prod --kubeconfig "${PROD_KUBECONFIG:-$HOME/.kubeconfigs/prod.yaml}" \
  --context "${PROD_CONTEXT:-prod}" > "$OUT/k8s-prod.jsonl" 2>/dev/null || echo "  prod skipped"

echo "==[7/11]== Ingest domain JSONL"
JAVA_FILES=$(ls "$OUT"/*.java.jsonl 2>/dev/null || true)
TS_FILES=$(ls "$OUT"/*.ts.jsonl 2>/dev/null || true)
PY_FILES=$(ls "$OUT"/*.py.jsonl 2>/dev/null || true)
[ -n "$JAVA_FILES" ] && for f in $JAVA_FILES; do "$PY" ingest_jsonl.py --jsonl "$f" --neo4j "$NEO4J"; done
# NOTE: ingest_jsonl.py currently java-focused; tsparser/pyparser JSONL use a
# compatible superset and will ingest classes/methods/functions+integrations.
[ -n "$TS_FILES" ] && for f in $TS_FILES; do "$PY" ingest_jsonl.py --jsonl "$f" --neo4j "$NEO4J" || true; done
[ -n "$PY_FILES" ] && for f in $PY_FILES; do "$PY" ingest_jsonl.py --jsonl "$f" --neo4j "$NEO4J" || true; done

echo "==[8/11]== Ingest meta + k8s + memory"
"$PY" ingest_repo_meta.py "$OUT/repo_meta.jsonl" --neo4j "$NEO4J"
[ -f "$OUT/k8s-qa.jsonl" ] && "$PY" ingest_k8s.py "$OUT/k8s-qa.jsonl" --neo4j "$NEO4J" || true
[ -f "$OUT/k8s-prod.jsonl" ] && "$PY" ingest_k8s.py "$OUT/k8s-prod.jsonl" --neo4j "$NEO4J" || true
"$PY" ingest_memory.py --neo4j "$NEO4J"

echo "==[9/11]== Linking passes"
"$PY" link_services.py --neo4j "$NEO4J"
"$PY" link_integrations.py --neo4j "$NEO4J"
"$PY" link_memories.py --neo4j "$NEO4J"

echo "==[10/11]== Embeddings (Method, Class, Endpoint, NatsSubject, MongoCollection, Memory)"
# bge-m3 TEI at :8764, 1024d. embed_nodes.py default --lm is LM Studio; override
# to point at the TEI container. If using LM Studio instead, set LM_URL +
# EMBED_MODEL accordingly.
"$PY" embed_nodes.py --neo4j "$NEO4J" --lm "${LM_URL:-http://127.0.0.1:8764/v1}" \
  --model "${EMBED_MODEL:-bge-m3}" --dim "${EMBED_DIM:-1024}"

echo "==[11/11]== Sanity"
bash bin/graph_sanity.sh
echo "DONE."
