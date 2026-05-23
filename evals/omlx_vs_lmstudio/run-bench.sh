#!/usr/bin/env bash
# Run end-to-end bench on MS. Driven from this file via `ssh ms bash -s`.
set -uo pipefail

RUN_ID="${RUN_ID:?RUN_ID required}"
export PATH="$HOME/.lmstudio/bin:$PATH"

cd "$HOME/omlx-eval"
. .venv/bin/activate
mkdir -p "results/$RUN_ID"

run_cell() {
  local server="$1" base="$2" model="$3" domain="$4" phase="$5"
  echo "--- [$server | $model | $domain | phase $phase] ---"
  python bench.py --server "$server" --base "$base" --model "$model" \
      --domain "$domain" --phase "$phase" --run-id "$RUN_ID" \
      2>&1 | tail -5 || echo "[warn] cell failed: $server $model $domain $phase"
  sleep 3
}

echo "==================== LM STUDIO ===================="
for domain in general coder; do
  case "$domain" in
    general) model="granite-4.1-30b" ;;
    coder)   model="qwen/qwen3-coder-next" ;;
  esac
  for phase in A B C; do
    run_cell lmstudio http://localhost:1234/v1 "$model" "$domain" "$phase"
  done
done

echo
echo "==================== UNLOAD LM STUDIO ===================="
lms unload --all 2>&1 || true
sleep 8
echo "free pages after unload:"
vm_stat | grep -E "free|inactive|speculative"

echo
echo "==================== OMLX ===================="
# Ensure omlx is serving with caches enabled
brew services list | grep -q "^omlx.*started" || brew services start jundot/omlx/omlx
sleep 3
curl -sf http://localhost:8000/v1/models >/dev/null || { echo "[fatal] omlx not up"; exit 1; }

for domain in general coder; do
  case "$domain" in
    general) model="granite-4.1-30b-4bit" ;;
    coder)   model="qwen3-coder-next-4bit" ;;
  esac
  for phase in A B C; do
    run_cell omlx http://localhost:8000/v1 "$model" "$domain" "$phase"
  done
done

echo
echo "==================== DONE ===================="
ls -la "results/$RUN_ID/"
