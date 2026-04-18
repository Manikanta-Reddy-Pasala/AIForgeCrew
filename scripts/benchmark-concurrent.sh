#!/usr/bin/env bash
# scripts/benchmark-concurrent.sh — P0 concurrent-pair bench.
# Measures sustained tok/s for realistic 2-agent concurrent usage
# (DESIGN lifecycle §4: Dev+Tester handoff, Tester+Arch handoff).
# Each request uses /v1/chat/completions streaming for per-token timing.
set -euo pipefail

ENDPOINT="${LLM_ENDPOINT:-http://localhost:1234/v1}"
OUT="${BENCH_OUT:-$HOME/aiforge-logs/bench-concurrent.txt}"
MAX_TOKENS="${MAX_TOKENS:-800}"
: > "$OUT"

QWEN="qwen3.6-35b-a3b"
GLM="zai-org/glm-4.7-flash"
GEMMA="gemma-4-31b-it"

PROMPT_DEV='Write a Python function `prime_factors(n)` returning list of prime factors of n. Include 3 pytest cases. Code only, no explanation.'
PROMPT_TESTER='Given a login form at /auth with inputs #email #password and button #submit, write Playwright tool calls (playwright_navigate / playwright_fill / playwright_click / playwright_get_text) to validate error "User not found" when email=nobody@x.com, password=test. Tool calls only, one per line.'
PROMPT_ARCH='Review this Python for bugs and security issues, reference line numbers, max 200 words.
```python
 1: import pickle, os
 2: from flask import Flask, request
 3: app = Flask(__name__)
 4: @app.route("/load", methods=["POST"])
 5: def load():
 6:     data = request.get_data()
 7:     obj = pickle.loads(data)
 8:     os.system(obj.get("cmd"))
 9:     return "ok"
10: app.run(host="0.0.0.0", port=5000, debug=True)
```'

single_call() {
  local label="$1" model="$2" prompt="$3"
  local tmp_resp tmp_meta dur tokens rate http
  tmp_resp=$(mktemp); tmp_meta=$(mktemp)
  local body
  body=$(jq -n --arg m "$model" --arg p "$prompt" --argjson mx "$MAX_TOKENS" \
    '{model:$m, messages:[{role:"user",content:$p}], stream:false, temperature:0.0, max_tokens:$mx}')
  curl -s -o "$tmp_resp" -w '%{time_total}|%{http_code}' \
    -X POST "$ENDPOINT/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "$body" > "$tmp_meta" || true
  dur=$(cut -d'|' -f1 "$tmp_meta")
  http=$(cut -d'|' -f2 "$tmp_meta")
  if [[ "$http" != "200" ]]; then
    printf '%s model=%s elapsed=%ss HTTP=%s body=%s\n' \
      "$label" "$model" "$dur" "$http" "$(head -c 200 "$tmp_resp")" | tee -a "$OUT"
    rm -f "$tmp_resp" "$tmp_meta"; return
  fi
  tokens=$(jq -r '.usage.completion_tokens // 0' "$tmp_resp" 2>/dev/null || echo 0)
  rate=$(awk -v t="$tokens" -v d="$dur" 'BEGIN{ if(d+0>0) printf "%.1f", t/d; else print "N/A"}')
  printf '%s model=%s elapsed=%ss tokens=%s tok/s=%s\n' "$label" "$model" "$dur" "$tokens" "$rate" | tee -a "$OUT"
  rm -f "$tmp_resp" "$tmp_meta"
}

warmup() {
  echo "=== WARMUP (load all 3 into memory) ===" | tee -a "$OUT"
  for m in "$QWEN" "$GLM" "$GEMMA"; do
    curl -s -o /dev/null -X POST "$ENDPOINT/chat/completions" \
      -H 'Content-Type: application/json' \
      -d "$(jq -n --arg m "$m" '{model:$m, messages:[{role:"user",content:"hi"}], max_tokens:5, temperature:0}')"
    echo "  warmed: $m" | tee -a "$OUT"
  done
}

solo() {
  echo -e "\n=== SOLO BASELINES (no concurrency) ===" | tee -a "$OUT"
  single_call "[solo-DEV]"    "$QWEN"  "$PROMPT_DEV"
  single_call "[solo-TESTER]" "$GLM"   "$PROMPT_TESTER"
  single_call "[solo-ARCH]"   "$GEMMA" "$PROMPT_ARCH"
}

pair() {
  local label="$1" m1="$2" p1="$3" m2="$4" p2="$5"
  echo -e "\n=== PAIR: $label ===" | tee -a "$OUT"
  local tmp1 tmp2
  tmp1=$(mktemp); tmp2=$(mktemp)
  single_call "[${label}-A]" "$m1" "$p1" > "$tmp1" 2>&1 &
  local pid1=$!
  single_call "[${label}-B]" "$m2" "$p2" > "$tmp2" 2>&1 &
  local pid2=$!
  wait $pid1 $pid2
  cat "$tmp1" "$tmp2" | tee -a "$OUT"
  rm -f "$tmp1" "$tmp2"
}

warmup
solo
pair "DEV+TESTER" "$QWEN" "$PROMPT_DEV" "$GLM"   "$PROMPT_TESTER"
pair "DEV+ARCH"   "$QWEN" "$PROMPT_DEV" "$GEMMA" "$PROMPT_ARCH"
pair "TESTER+ARCH" "$GLM"  "$PROMPT_TESTER" "$GEMMA" "$PROMPT_ARCH"

echo -e "\n=== DONE ($OUT) ===" | tee -a "$OUT"
