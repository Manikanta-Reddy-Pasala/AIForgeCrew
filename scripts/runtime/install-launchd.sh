#!/usr/bin/env bash
# Install the graph-runner LaunchAgent + daily reindex + watchdogs. Idempotent.
# Runs ON Mac Studio.
#
# The LangGraph graph-runner replaces the old per-role tick plists.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS only" >&2; exit 1; }

SRC="${SRC:-$HOME/AIForgeCrew/scripts/runtime}"
DST="$HOME/Library/LaunchAgents"

mkdir -p "$HOME/.aiforge/logs"
mkdir -p "$DST"

echo ">>> stopping legacy LaunchAgents"
for legacy in \
  com.aiforge.paperclip \
  com.aiforge.paperclip-tunnel \
  com.aiforge.hermes-dashboard \
  com.aiforge.tick-architect \
  com.aiforge.tick-sr_developer \
  com.aiforge.tick-developer \
  com.aiforge.tick-fact_extract \
  com.aiforge.tick-supervisor \
  com.aiforge.tick-planner \
  com.aiforge.tick-planner-b \
  com.aiforge.tick-doer \
  com.aiforge.tick-doer-b \
  com.aiforge.tick-feedback \
  com.aiforge.tick-learner
do
  launchctl bootout "gui/$(id -u)/${legacy}" 2>/dev/null || true
  rm -f "${DST}/${legacy}.plist" 2>/dev/null || true
done

echo ">>> installing graph-runner LaunchAgent"
for label in com.aiforge.graph-runner; do
  src="${SRC}/${label}.plist"
  dst="${DST}/${label}.plist"
  [[ -f "$src" ]] || { echo "missing $src"; continue; }
  cp "$src" "$dst"
  launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dst"
  echo "  loaded $label"
done

echo ">>> installing watchdogs (postgres + git-pull + file-indexer + daily reindex)"
for label in com.aiforge.pg-watchdog com.aiforge.git-pull com.aiforge.file-indexer com.aiforge.reindex-daily; do
  src="${SRC}/${label}.plist"
  dst="${DST}/${label}.plist"
  [[ -f "$src" ]] || { echo "missing $src"; continue; }
  cp "$src" "$dst"
  launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dst"
  echo "  loaded $label"
done

echo
echo "Verify:"
echo "  launchctl list | grep aiforge"
echo "  tail -f ~/.aiforge/logs/orchestrator-*.ndjson | jq -c '{ts,role,ticket,event,tool,dur_ms}'"
