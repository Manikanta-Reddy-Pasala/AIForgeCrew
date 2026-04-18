#!/usr/bin/env bash
# scripts/benchmark-passk.sh — pass@1 harness for the Dev role.
# For each eval ticket under docs/eval/tickets/, ask the configured Dev model
# to produce code, save it, run the expected tests, record pass/fail.
#
# Runs over SSH against the Mac Studio LM Studio server so no LLM deps are
# needed on the driving machine.
set -euo pipefail

ENDPOINT="${LLM_ENDPOINT:-http://localhost:1234/v1}"
MODEL="${MODEL:-qwen3.6-35b-a3b}"
EVAL_DIR="${EVAL_DIR:-docs/eval/tickets}"
OUT="${OUT:-$HOME/aiforge-logs/passk.tsv}"
mkdir -p "$(dirname "$OUT")"
: > "$OUT"

UV="${UV:-$HOME/.local/bin/uv}"; command -v "$UV" >/dev/null || UV=uv

total=0
passed=0

extract_code() {
  # Strip fenced code block markers if present, else pass through.
  "$UV" run --quiet --with pyyaml python - <<'PY'
import re, sys
text = sys.stdin.read()
m = re.search(r"```(?:python)?\n(.*?)```", text, re.S)
print(m.group(1) if m else text)
PY
}

printf "ticket\tmodel\tpass@1\n" | tee -a "$OUT"

for f in "$EVAL_DIR"/*.yml; do
  total=$((total+1))
  TID=$(basename "$f" .yml)
  PROMPT=$("$UV" run --quiet --with pyyaml python -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['prompt'])" "$f")
  TESTS=$("$UV" run --quiet --with pyyaml python -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['expected_tests'])" "$f")

  body=$(jq -n --arg m "$MODEL" --arg p "$PROMPT" \
    '{model:$m, messages:[{role:"user",content:$p}], stream:false, temperature:0, max_tokens:2000}')
  resp=$(curl -sS -X POST "$ENDPOINT/chat/completions" -H 'Content-Type: application/json' -d "$body")
  content=$(echo "$resp" | jq -r '.choices[0].message.content // .choices[0].message.reasoning_content // ""')
  code=$(printf '%s' "$content" | extract_code)

  workdir=$(mktemp -d)
  mkdir -p "$workdir/src" "$workdir/tests"
  printf '%s\n' "$code" > "$workdir/src/$(basename "$TID")_impl.py"
  # Derive the src file name from the prompt's `src/..._utils.py` reference.
  src_hint=$(printf '%s' "$PROMPT" | grep -oE 'src/[a-zA-Z0-9_]+\.py' | head -1)
  if [[ -n "$src_hint" ]]; then
    mkdir -p "$workdir/$(dirname "$src_hint")"
    cp "$workdir/src/$(basename "$TID")_impl.py" "$workdir/$src_hint"
  fi
  printf '%s\n' "$TESTS" > "$workdir/tests/test_$TID.py"
  printf 'import sys; sys.path.insert(0, ".")\n' > "$workdir/tests/conftest.py"

  if (cd "$workdir" && "$UV" run --quiet --with pytest python -m pytest tests -q 2>/dev/null); then
    printf "%s\t%s\tPASS\n" "$TID" "$MODEL" | tee -a "$OUT"
    passed=$((passed+1))
  else
    printf "%s\t%s\tFAIL\n" "$TID" "$MODEL" | tee -a "$OUT"
  fi
  rm -rf "$workdir"
done

pct=0
if [[ $total -gt 0 ]]; then
  pct=$(awk -v p="$passed" -v t="$total" 'BEGIN{ printf "%.1f", 100.0*p/t }')
fi
echo "---"
echo "pass@1 = $passed/$total ($pct%)  model=$MODEL"
echo "pass@1 = $passed/$total ($pct%)  model=$MODEL" >> "$OUT"
