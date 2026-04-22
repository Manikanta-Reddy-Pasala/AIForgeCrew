#!/usr/bin/env bash
# Install the tick-<role> LaunchAgents + daily reindex. Idempotent.
# Runs ON Mac Studio.
#
# Roles (current):  supervisor  planner  doer  feedback  learner
# Legacy roles (architect/sr_developer/developer/fact_extract) are unloaded
# here — they still work via aliases inside the orchestrator, but the
# launchd agents are renamed to the canonical role names.
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
  com.aiforge.tick-fact_extract
do
  launchctl bootout "gui/$(id -u)/${legacy}" 2>/dev/null || true
  rm -f "${DST}/${legacy}.plist" 2>/dev/null || true
done

echo ">>> installing tick LaunchAgents (5-role pipeline + 2nd doer worker)"
for label_slug in supervisor planner planner-b doer doer-b feedback learner; do
  label="com.aiforge.tick-${label_slug}"
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
