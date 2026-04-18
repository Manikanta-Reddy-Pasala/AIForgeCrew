#!/usr/bin/env bash
# scripts/health-check.sh — probe local services + inference per role.
# P0: LM Studio + per-role inference probe.
# P2+ will add paperclip/hermes/mem0/rag probes.
set -euo pipefail

ENDPOINT="${LLM_ENDPOINT:-http://localhost:1234/v1}"
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) echo "Usage: health-check.sh [--dry-run]"; exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

status=0
probe_url() {
  local name="$1" url="$2"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "would probe ${name} @ ${url}"
    return 0
  fi
  if curl -fsS --max-time 3 "${url}" >/dev/null 2>&1; then
    echo "OK   ${name}"
  else
    echo "FAIL ${name} (${url})" >&2
    status=1
  fi
}

# Basic server up.
probe_url "lmstudio-server" "${ENDPOINT}/models"
[[ $DRY_RUN -eq 1 ]] && { echo "dry-run OK"; exit 0; }

# Per-role model-ID regex (matches benchmark-models.sh).
MODELS_JSON=$(curl -fsS --max-time 3 "${ENDPOINT}/models" 2>/dev/null || echo '{}')
dev_id=$(echo "$MODELS_JSON"   | jq -r '.data[]?.id // empty' | grep -iE 'qwen3?.?6.?35b|qwen.*35b.?a3b' | head -1)
tester_id=$(echo "$MODELS_JSON" | jq -r '.data[]?.id // empty' | grep -iE 'glm.?4.?7.?flash'              | head -1)
arch_id=$(echo "$MODELS_JSON"   | jq -r '.data[]?.id // empty' | grep -iE 'gemma.?4.?31b'                 | head -1)

probe_role() {
  local role="$1" model="$2"
  if [[ -z "$model" ]]; then
    echo "SKIP ${role} (no model matched)"
    status=1
    return
  fi
  local resp
  resp=$(curl -fsS --max-time 120 -X POST "${ENDPOINT}/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg m "$model" '{model:$m, messages:[{role:"user",content:"ping"}], max_tokens:16, temperature:0}')" 2>/dev/null || echo '{}')
  # Accept response if EITHER content OR reasoning_content is non-empty (thinking-mode models).
  if echo "$resp" | jq -e '.choices[0].message | ((.content // "") != "" or (.reasoning_content // "") != "")' >/dev/null 2>&1; then
    echo "OK   ${role} (${model})"
  else
    echo "FAIL ${role} (${model}) → ${resp:0:120}" >&2
    status=1
  fi
}

probe_role "dev"    "$dev_id"
probe_role "tester" "$tester_id"
probe_role "arch"   "$arch_id"

exit $status
