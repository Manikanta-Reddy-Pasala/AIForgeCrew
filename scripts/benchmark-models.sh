#!/usr/bin/env bash
# scripts/benchmark-models.sh — P0 model benchmark harness.
# Runs three representative tasks against loaded LM Studio MLX models,
# measures elapsed seconds + tok/s, dumps response body.
#
# Prereq: LM Studio server running on :1234 (OpenAI-compat).
# Models are lazy-loaded by LM Studio on first /v1/chat/completions call.
# Run on Mac Studio: ssh manikanta@192.168.70.185 'bash -s' < scripts/benchmark-models.sh
set -euo pipefail

ENDPOINT="${LLM_ENDPOINT:-http://localhost:1234/v1}"
OUT="${BENCH_OUT:-$HOME/aiforge-logs/bench.txt}"
MAX_TOKENS="${MAX_TOKENS:-3000}"   # room for thinking-mode models
: > "$OUT"

bench() {
  local role="$1" model="$2" prompt="$3"
  local body tmp_resp tmp_meta dur_s tokens reasoning_tokens content tok_s

  body=$(jq -n --arg m "$model" --arg p "$prompt" --argjson mx "$MAX_TOKENS" \
    '{model:$m, messages:[{role:"user",content:$p}], stream:false, temperature:0.0, max_tokens:$mx}')

  tmp_resp=$(mktemp)
  tmp_meta=$(mktemp)

  # curl writes body to tmp_resp; -w prints timing to stdout (captured in tmp_meta).
  curl -s -o "$tmp_resp" -w '%{time_total}' \
    -X POST "$ENDPOINT/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "$body" > "$tmp_meta"

  dur_s=$(cat "$tmp_meta")
  tokens=$(jq -r '.usage.completion_tokens // 0' "$tmp_resp")
  reasoning_tokens=$(jq -r '.usage.completion_tokens_details.reasoning_tokens // 0' "$tmp_resp")
  content=$(jq -r '
    .choices[0].message as $m |
    if ($m.content // "") != "" then $m.content
    elif ($m.reasoning_content // "") != "" then "[REASONING_ONLY]\n" + $m.reasoning_content
    else (.error // (. | tostring))
    end' "$tmp_resp")

  # tok/s via awk (no python)
  tok_s=$(awk -v t="$tokens" -v d="$dur_s" 'BEGIN{ if(d+0>0) printf "%.1f", t/d; else print "N/A"}')

  {
    printf '\n== %s [%s] ==\n' "$role" "$model"
    printf 'elapsed_s=%s tokens=%s reasoning_tokens=%s tok/s=%s\n' \
      "$dur_s" "$tokens" "$reasoning_tokens" "$tok_s"
    printf 'response:\n%s\n' "$content"
  } | tee -a "$OUT"

  rm -f "$tmp_resp" "$tmp_meta"
}

# ---- Dev prompt: make failing test pass ----
DEV_PROMPT='Here is a failing Python test. Write ONLY the function in src/math_utils.py that makes it pass.

# tests/test_math_utils.py
from src.math_utils import fibonacci

def test_fib_base():
    assert fibonacci(0) == 0
    assert fibonacci(1) == 1

def test_fib_recursive():
    assert fibonacci(10) == 55

def test_fib_large():
    assert fibonacci(30) == 832040

Return only the Python code, no explanation.'

# ---- Tester prompt: browser MCP tool-use planning ----
TESTER_PROMPT='You are a QA agent with access to these tools:
- playwright_navigate(url)
- playwright_click(selector)
- playwright_fill(selector, value)
- playwright_screenshot()
- playwright_get_text(selector)

Task: validate that on https://example-app.test/login, entering user "alice" password "wrong" shows error "Invalid credentials" within 2 seconds. Output the exact ordered list of tool calls (one per line) you would make. No prose.'

# ---- Architect prompt: code review ----
ARCH_PROMPT='Review this code for bugs, security issues, and architectural problems. Be concise (max 150 words) and reference line numbers.

```python
 1: import sqlite3
 2: from flask import Flask, request
 3: app = Flask(__name__)
 4:
 5: @app.route("/user")
 6: def get_user():
 7:     uid = request.args.get("id")
 8:     conn = sqlite3.connect("app.db")
 9:     cur = conn.cursor()
10:     cur.execute(f"SELECT name, email FROM users WHERE id = {uid}")
11:     row = cur.fetchone()
12:     return {"name": row[0], "email": row[1]}
13:
14: if __name__ == "__main__":
15:     app.run(debug=True, host="0.0.0.0")
```'

echo "Fetching available models..." | tee -a "$OUT"
MODELS_JSON=$(curl -s "$ENDPOINT/models")
echo "$MODELS_JSON" | jq -r '.data[].id' | tee -a "$OUT"

DEV_MODEL=$(echo "$MODELS_JSON"   | jq -r '.data[].id' | grep -iE 'qwen3?.?6.?35b|qwen.*35b.?a3b' | head -1)
TESTER_MODEL=$(echo "$MODELS_JSON" | jq -r '.data[].id' | grep -iE 'glm.?4.?7.?flash'              | head -1)
ARCH_MODEL=$(echo "$MODELS_JSON"   | jq -r '.data[].id' | grep -iE 'gemma.?4.?31b'                 | head -1)

[[ -n "$DEV_MODEL"    ]] && bench "Sr Developer" "$DEV_MODEL"    "$DEV_PROMPT"    || echo "WARN: Dev model not loaded"    | tee -a "$OUT"
[[ -n "$TESTER_MODEL" ]] && bench "Tester"       "$TESTER_MODEL" "$TESTER_PROMPT" || echo "WARN: Tester model not loaded" | tee -a "$OUT"
[[ -n "$ARCH_MODEL"   ]] && bench "Sr Architect" "$ARCH_MODEL"   "$ARCH_PROMPT"   || echo "WARN: Arch model not loaded"   | tee -a "$OUT"

echo -e "\n==== bench complete: $OUT ====" | tee -a "$OUT"
