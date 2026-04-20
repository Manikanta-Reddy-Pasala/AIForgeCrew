#!/usr/bin/env bash
# scripts/stock-transfer-analysis.sh — Ask both Sr Dev agents (Reasoning + Coder)
# to investigate stock transfer flow and identify issues. Sequential due to
# Mac Studio RAM constraint. Outputs written to docs/eval/stock-transfer-{model}.md.
set -uo pipefail

SSH_HOST="${1:-manikanta@192.168.70.185}"
TURNS=200

remote() { ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST" "$@"; }

declare -a RUNS=(
  # model|ctx|suffix|label_name
  "gemma-4-31b-it|131072|reasoning|Sr Dev (Reasoning)"
  "qwen3-coder-next|65536|coder|Sr Dev (Coder)"
)

PROMPT_LOCAL=$(mktemp)
cat > "$PROMPT_LOCAL" <<'EOF'
You are the Senior Developer on AIForgeCrew. This is an INVESTIGATION task, not an implementation task. Do NOT write code or create branches. Produce an ANALYSIS DOCUMENT.

TASK: Identify issues in the stock transfer flow.

Scope:
  1. Store-to-store transfer
  2. Warehouse-to-warehouse transfer
  (both, separately — they may share or diverge on rules)

REPO ON DISK: /Users/manikanta/codeRepo/MongoDbService

## MANDATORY — you MUST use these layers first before reading files:

STEP 1. Invoke the aiforge-rag skill to find relevant code:
  skill_view("aiforge-rag")  (if not already loaded)

  Then run these queries via the skill's Python interface:
    - "stock transfer between stores"
    - "stock transfer warehouse"
    - "TransferStockService"
    - "ProductServiceImpl warehouseDetails"
    - "inventory adjustment atomic"

  Cite the file paths returned (use the `source:` prefix to know which repo).

STEP 2. Read your AGENTS.md CODEBASE_INDEX section (already in your system prompt) to know the repo layout.

STEP 3. Call hindsight_recall with:
  - "stock transfer flow"
  - "transferStock bugs"
  - "warehouseDetails array MongoDB"
  Log what was retrieved (zero facts is fine — just log it).

ONLY THEN read source files. Focus area:
  ~/codeRepo/MongoDbService/src/main/java/com/oneshell/mongodb/feature/transferStock/
  ~/codeRepo/MongoDbService/src/main/java/com/oneshell/mongodb/feature/dao/transferStock/
  ~/codeRepo/MongoDbService/src/main/java/com/oneshell/mongodb/feature/product/ProductServiceImpl.java  (STOCK-TRANSFER-ATOMIC sections)

## Deliverable

Write the analysis to: /Users/manikanta/codeRepo/MongoDbService/docs/stock-transfer-analysis-<SUFFIX>.md

Where <SUFFIX> = your model slug (e.g. "reasoning" or "coder"). Include:

1. **Flow diagrams (ascii or mermaid)** — separate diagrams for store-to-store and warehouse-to-warehouse.
2. **Issues found** — table with columns: #, severity (blocker/high/med/low), category (bug|race|validation|schema|test-gap|perf|consistency), summary, file:line evidence.
3. **Cited RAG hits** — paste the top-3 snippets from aiforge-rag queries so we see your retrieval trail.
4. **Hindsight results** — paste the hindsight_recall output for each query.
5. **Gaps** — what would you need to fully validate the issues (e.g. test fixtures, prod logs, sample data).
6. **Recommended fix order** — rank issues by cost/impact.

## Non-negotiables

- Use aiforge-rag BEFORE reading any source file. If you skip it, your analysis is rejected.
- Cite file:line for every issue. Vague claims without path are rejected.
- Separate store-to-store issues from warehouse-to-warehouse issues clearly.
- No code changes. No branches. No commits. Just the markdown file.
- Keep analysis under 800 lines. Focus on real issues, not style nits.

When done, print:
  ANALYSIS_WRITTEN: <full path to markdown>

You have up to 200 turns. No wall clock limit.
EOF

for entry in "${RUNS[@]}"; do
  IFS='|' read -r MODEL CTX SUFFIX AGENT_NAME <<< "$entry"
  echo "============================================================"
  echo " $AGENT_NAME analysis — model=$MODEL ctx=$CTX"
  echo "============================================================"

  # Unload + load the right model
  remote "\$HOME/.lmstudio/bin/lms unload --all 2>&1 | tail -2; sleep 3"
  LOAD=$(remote "\$HOME/.lmstudio/bin/lms load -y '$MODEL' -c $CTX --gpu max 2>&1 | tail -3" || true)
  echo "  load: $(echo "$LOAD" | tr '\n' ' ' | tail -c 120)"
  if ! echo "$LOAD" | grep -q "loaded successfully\|To use the model"; then
    echo "  SKIP $MODEL — load failed"
    continue
  fi

  # Push per-run prompt (sub in suffix)
  PROMPT_REMOTE="/tmp/stock-transfer-prompt-${SUFFIX}.txt"
  sed "s/<SUFFIX>/$SUFFIX/g" "$PROMPT_LOCAL" > "${PROMPT_LOCAL}.sub"
  scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "${PROMPT_LOCAL}.sub" "$SSH_HOST:$PROMPT_REMOTE"
  rm -f "${PROMPT_LOCAL}.sub"

  HERMES_LOG="/tmp/hermes-stock-transfer-${SUFFIX}.log"
  echo "  launching hermes (max-turns=$TURNS, NO gtimeout)..."
  START=$(date +%s)
  remote "cd ~/codeRepo/MongoDbService && ~/.local/bin/hermes chat --max-turns $TURNS --yolo --source tool -Q -m '$MODEL' -q \"\$(cat $PROMPT_REMOTE)\" > $HERMES_LOG 2>&1; echo EXIT=\$?"
  END=$(date +%s); WALL=$((END - START))
  echo "  wall: ${WALL}s"

  # Pull the analysis back
  OUT_REMOTE="/Users/manikanta/codeRepo/MongoDbService/docs/stock-transfer-analysis-${SUFFIX}.md"
  OUT_LOCAL="docs/eval/stock-transfer-${SUFFIX}.md"
  mkdir -p docs/eval
  if remote "test -f $OUT_REMOTE"; then
    scp -q -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 "$SSH_HOST:$OUT_REMOTE" "$OUT_LOCAL"
    echo "  fetched: $OUT_LOCAL ($(wc -l < "$OUT_LOCAL") lines)"
  else
    echo "  NO ANALYSIS FILE WRITTEN on remote. Check $HERMES_LOG"
  fi
  echo
done

rm -f "$PROMPT_LOCAL"

echo "=============== stock transfer analysis done ==============="
ls -la docs/eval/stock-transfer-*.md 2>/dev/null | tail
