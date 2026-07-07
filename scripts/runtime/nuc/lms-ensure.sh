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
#   AIFORGE_LMS_CTX     legacy ctx           (default 262144 = 256K, matches load-models.sh)
#   AIFORGE_LMS_TTL     legacy ttl seconds   (default 43200)
set -euo pipefail

# Opt-in: this manages a REMOTE LM Studio host. No default host — a generic
# clone that never sets AIFORGE_LMS_HOST skips this entirely (this box's IP is
# operator config, not a shared-repo constant).
HOST="${AIFORGE_LMS_HOST:-}"
if [ -z "$HOST" ]; then
    echo "lms-ensure: AIFORGE_LMS_HOST unset — skipping (no remote LM Studio to manage)"
    exit 0
fi
BIN="${AIFORGE_LMS_BIN:-lms}"
LEGACY_MODEL="${AIFORGE_LMS_MODEL:-qwen/qwen3-coder-next}"
LEGACY_CTX="${AIFORGE_LMS_CTX:-262144}"
LEGACY_TTL="${AIFORGE_LMS_TTL:-43200}"
# Concurrent predictions per model. At a big context a 30B+ model's KV cache is
# multi-GB per slot; parallel>1 at 256K can exceed VRAM and make LM Studio drop
# to a tiny context. Default 1 (serial, stable). Raise only with headroom.
PARALLEL="${AIFORGE_LMS_PARALLEL:-1}"
SPECS="${AIFORGE_LMS_MODELS:-${LEGACY_MODEL}:${LEGACY_CTX}:${LEGACY_TTL}}"

# Disable LM Studio Just-In-Time model loading — the "8192 trap". With JIT ON,
# a request for a model auto-loads a SECOND instance under the base id at the
# tiny default context (4096/8192), which then SERVES every request while our
# explicit full-context load sits under a ':2' id, ignored — so every agent
# 400s "tokens to keep from initial prompt > context length". Turning JIT off
# means only our explicit --context-length load exists. Idempotent (no-op +
# no server restart once already false).
ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" '
  CFG="$HOME/.lmstudio/.internal/http-server-config.json"
  if [ -f "$CFG" ] && grep -q "\"justInTimeModelLoading\": true" "$CFG"; then
    sed -i.bak "s/\"justInTimeModelLoading\": true/\"justInTimeModelLoading\": false/" "$CFG"
    '"$BIN"' server stop  >/dev/null 2>&1 || true
    '"$BIN"' server start >/dev/null 2>&1 || true
    echo "lms-ensure: disabled JIT model loading + restarted server"
  fi
' 2>/dev/null || echo "lms-ensure: JIT-disable step skipped (host unreachable)" >&2

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
         $BIN load '$MODEL' --context-length $CTX --parallel $PARALLEL --ttl $TTL --quiet" \
        && echo "lms-ensure: reloaded $MODEL ctx=$CTX parallel=$PARALLEL ttl=${TTL}s" \
        || { echo "lms-ensure: reload FAILED for $MODEL" >&2; rc=1; }
done
exit $rc
