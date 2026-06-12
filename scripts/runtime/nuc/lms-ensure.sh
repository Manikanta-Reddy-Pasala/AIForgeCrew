#!/usr/bin/env bash
# Ensure the required LM Studio models on the Mac Studio are loaded with
# the REQUIRED context length + TTL.
#
# Why: LM Studio JIT-loads an unloaded model on first request with
# server defaults (ctx=4096, ttl=1h). The v6 Doer prompt alone exceeds
# 4096, so every JIT load = instant 400 ("tokens to keep > context
# length") and the local pass dies until something reloads the model
# properly (ONE-93 first-pass failure). After every idle-unload the
# poison path recurs — so this runs on a timer and reloads whenever a
# loaded ctx is wrong or a model is missing.
#
# Multi-model (2026-06-13): the box runs the Doer (qwen3-coder-next)
# AND the judge/reviewer (nex-n2-mini) side by side (45G + 20G on a
# 96G box). Spec list format:
#   AIFORGE_LMS_MODELS="model:ctx:ttl,model:ctx:ttl"
# Falls back to the legacy single-model envs when unset.
#
# Env (same family as runtime/local_starter.py):
#   AIFORGE_LMS_HOST    ssh target           (default manikanta@192.168.70.185)
#   AIFORGE_LMS_BIN     lms path on the host (default ~/.lmstudio/bin/lms)
#   AIFORGE_LMS_MODELS  spec list, see above
#   AIFORGE_LMS_MODEL   legacy single model  (default qwen/qwen3-coder-next)
#   AIFORGE_LMS_CTX     legacy ctx           (default 131072)
#   AIFORGE_LMS_TTL     legacy ttl seconds   (default 43200)
set -euo pipefail

HOST="${AIFORGE_LMS_HOST:-manikanta@192.168.70.185}"
BIN="${AIFORGE_LMS_BIN:-/Users/manikanta/.lmstudio/bin/lms}"
LEGACY_MODEL="${AIFORGE_LMS_MODEL:-qwen/qwen3-coder-next}"
LEGACY_CTX="${AIFORGE_LMS_CTX:-131072}"
LEGACY_TTL="${AIFORGE_LMS_TTL:-43200}"
SPECS="${AIFORGE_LMS_MODELS:-${LEGACY_MODEL}:${LEGACY_CTX}:${LEGACY_TTL}}"

state=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$BIN ps --json" \
        2>/dev/null || echo "[]")

rc=0
IFS=',' read -ra SPEC_ARR <<< "$SPECS"
for spec in "${SPEC_ARR[@]}"; do
    spec="$(echo "$spec" | xargs)"   # trim
    [ -n "$spec" ] || continue
    MODEL="${spec%%:*}"
    rest="${spec#*:}"
    CTX="${rest%%:*}"
    TTL="${rest#*:}"
    [ "$CTX" != "$spec" ] || CTX="$LEGACY_CTX"
    [ "$TTL" != "$rest" ] || TTL="$LEGACY_TTL"

    # JSON handed over via env var — piping to stdin clashes with
    # reading the inline script from stdin (the original heredoc bug
    # made the parse always fail -> ctx=0 -> reload on every tick,
    # double-loading the model).
    loaded_ctx=$(LMS_STATE="$state" LMS_MODEL_ID="$MODEL" python3 -c '
import json, os
model = os.environ["LMS_MODEL_ID"]
try:
    rows = json.loads(os.environ.get("LMS_STATE") or "[]")
except Exception:
    rows = []
best = 0
for r in rows:
    if r.get("identifier") == model or r.get("modelKey") == model:
        best = max(best, int(r.get("contextLength") or 0))
print(best)
')

    if [ "$loaded_ctx" -ge "$CTX" ] 2>/dev/null; then
        echo "lms-ensure: $MODEL loaded ctx=$loaded_ctx (>= $CTX) — ok"
        continue
    fi

    echo "lms-ensure: $MODEL ctx=$loaded_ctx < required $CTX — reloading"
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" \
        "$BIN unload '$MODEL' >/dev/null 2>&1 || true; \
         $BIN load '$MODEL' --context-length $CTX --ttl $TTL --quiet" \
        && echo "lms-ensure: reloaded $MODEL ctx=$CTX ttl=${TTL}s" \
        || { echo "lms-ensure: reload FAILED for $MODEL" >&2; rc=1; }
done
exit $rc
