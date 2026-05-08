#!/usr/bin/env bash
# lms-pin-watchdog: keep both AIForge LLMs loaded at their pinned context length.
#
# LM Studio JIT-loads models on first API request using the default context
# (4K), which silently replaces our 512K-pinned instance. The bootout/restart
# of any consumer (graph-runner, api) can also race the unload. This script
# checks `lms ps` every 60s and re-pins any model that drifted.
#
# Pins:
#   qwen3.6-27b      @ 524288 ctx   (Planner / Feedback / Learner / Supervisor)
#   qwen3.6-35b-a3b  @ 524288 ctx   (Doer)
#
# TTL 43200s = 12h, matching the normal pinned lifetime.
#
# Runs forever; restart via launchctl kickstart.

set -u
PATH="$HOME/.lmstudio/bin:$PATH"
# Pin the active local LLM. As of 2026-04-24 we consolidated from
# (qwen3.6-27b + qwen3.6-35b-a3b @ 512K each) down to a single
# gpt-oss-120b @ 128K that handles every role (planner/doer/feedback/
# learner/chat). OpenAI's gpt-oss supports up to 128K native context.
TARGET_CTX=262144   # qwen-coder-next (Qwen3-Coder-Next MLX 80B @ 4bit,
                    # ~45GB weights + 256K KV ≈ 70GB on 96GB Mac Studio).
                    # 256K is the contractual default — 64K truncated the
                    # ONE-116 3kLOC multi-turn Doer mid-run.
TARGET_TTL=0        # 0 = no TTL → keep loaded until explicit ``lms unload``.
                    # Earlier 12h/24h finite TTLs were observed to drop the
                    # model mid-run when idle gaps between turns tripped
                    # LM Studio's idle-unload heuristic. Operator-driven
                    # lifetime is the safer default for long tickets.
MODELS=(qwen-coder-next)
INTERVAL=60

log() { printf '%s lms-pin: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

ensure_pinned() {
  local name=$1
  # gpt-oss-120b lives under openai/gpt-oss-120b in the LM Studio
  # registry but runs with bare identifier gpt-oss-120b. Resolve the
  # right spec for `lms load`.
  local load_spec="$name"
  case "$name" in
    gpt-oss-120b)        load_spec="openai/gpt-oss-120b" ;;
    glm-4.7-flash)       load_spec="zai-org/glm-4.7-flash" ;;
    gemma-4-31b-it-mlx)  load_spec="lmstudio-community/gemma-4-31B-it-MLX-8bit" ;;
    qwen-coder-next)     load_spec="qwen/qwen3-coder-next" ;;
  esac
  local row ctx
  row=$(lms ps 2>/dev/null | awk -v n="$name" '$1 == n { print $0 }')
  if [ -z "$row" ]; then
    log "$name not loaded — loading at $TARGET_CTX (ttl=$TARGET_TTL)"
    if [ "$TARGET_TTL" -gt 0 ]; then
      lms load "$load_spec" --context-length "$TARGET_CTX" --ttl "$TARGET_TTL" \
        --identifier "$name" >/dev/null 2>&1 &
    else
      # No --ttl flag → LM Studio keeps the model loaded until an
      # explicit ``lms unload``. Matches local_starter's default.
      lms load "$load_spec" --context-length "$TARGET_CTX" \
        --identifier "$name" >/dev/null 2>&1 &
    fi
    return
  fi
  # lms ps columns (whitespace-separated):
  #  1 IDENTIFIER  2 MODEL  3 STATUS  4 SIZE-num  5 SIZE-unit
  #  6 CONTEXT  7 PARALLEL  8 DEVICE  9+ TTL
  # CONTEXT = field 6 when SIZE is "16.08 GB" (two tokens).
  ctx=$(echo "$row" | awk '{ print $6 }')
  if [ "$ctx" != "$TARGET_CTX" ]; then
    log "$name ctx=$ctx != $TARGET_CTX — re-pinning"
    lms unload "$name" >/dev/null 2>&1 || true
    sleep 2
    lms load "$load_spec" --context-length "$TARGET_CTX" --ttl "$TARGET_TTL" \
      --identifier "$name" >/dev/null 2>&1 &
  fi
}

log "watchdog starting — target ctx=$TARGET_CTX, interval=${INTERVAL}s"
while true; do
  for m in "${MODELS[@]}"; do
    ensure_pinned "$m"
  done
  sleep "$INTERVAL"
done
