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
TARGET_CTX=524288
TARGET_TTL=43200
MODELS=(qwen3.6-27b qwen3.6-35b-a3b)
INTERVAL=60

log() { printf '%s lms-pin: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

ensure_pinned() {
  local name=$1
  local row ctx
  row=$(lms ps 2>/dev/null | awk -v n="$name" '$1 == n { print $0 }')
  if [ -z "$row" ]; then
    log "$name not loaded — loading at $TARGET_CTX"
    lms load "$name" --context-length "$TARGET_CTX" --ttl "$TARGET_TTL" \
      --identifier "$name" >/dev/null 2>&1 &
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
    lms load "$name" --context-length "$TARGET_CTX" --ttl "$TARGET_TTL" \
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
